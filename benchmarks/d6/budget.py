"""Rotate immutable 100-attempt ledgers between formal episodes, never mid-call."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cloudsre.durable import now
from cloudsre.model_budget import ModelBudget
from dotenv import dotenv_values

from benchmarks.d5.continue_batch import no_inflight
from benchmarks.d5.live import exclusive

from .protocol import ROOT, WORK

DIRECT = ROOT / "evaluation/d5-runtime/direct-proxy-deployment.json"
BUDGETS = ROOT / "evaluation/d5-runtime/budget-private"


def current():
    deployment = json.loads(DIRECT.read_text())
    pid = int(deployment["host_pid"])
    cmd = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    if b"uvicorn" not in cmd or b"proxy_observer:configured_app" not in cmd:
        raise ValueError("Unverified proxy process identity")
    # Read only the active ledger path; never return or log process credentials.
    entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    path = next(
        x.split(b"=", 1)[1].decode()
        for x in entries
        if x.startswith(b"D5_BUDGET_DATABASE=")
    )
    database = Path(path).resolve()
    if database.parent != BUDGETS.resolve():
        raise ValueError("Unexpected proxy ledger location")
    summary = ModelBudget(database, 100).summary()
    no_inflight(summary)
    health = httpx.get("http://localhost:18090/health", timeout=10).json()
    if any(
        health.get(k) != summary[k] for k in ("limit", "used_slots", "remaining_slots")
    ):
        raise ValueError("Relay ledger mismatch")
    return database, deployment


def rotate():
    with exclusive():
        old, prior = current()
        if ModelBudget(old, 100).summary()["remaining_slots"] >= 4:
            raise ValueError("Current ledger still fits another full model task")
        numbers = [
            int(p.stem[5:])
            for p in BUDGETS.glob("batch*.sqlite")
            if p.stem[5:].isdigit()
        ]
        index = max(numbers) + 1
        new = BUDGETS / f"batch{index}.sqlite"
        ModelBudget(new, 100)
        auth = ROOT / "evaluation/d5-private" / f"batch{index}-authorization.json"
        auth.write_text(
            json.dumps(
                {
                    "batch_id": f"b{index}",
                    "limit": 100,
                    "user_approved": True,
                    "approved_at": now(),
                    "source_reply": "用户已解除累计模型调用上限，并允许项目演练场景持续调用；本轮要求执行D6正式验证",
                    "global_total_limit": None,
                    "cumulative_cap_removed": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        auth.chmod(0o600)
        model = dotenv_values("/mnt/d/uclaude/model.env")
        if urlsplit(model["OPENAI_BASE_URL"]).hostname != "api.deepseek.com":
            raise ValueError("Unexpected model destination")
        env = {
            k: v
            for k, v in os.environ.items()
            if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        env.update(
            {k: model[k] for k in ("OPENAI_BASE_URL", "DEEPSEEK_API_KEY", "MODEL_NAME")}
        )
        env.update(
            D5_BUDGET_DATABASE=str(new),
            D5_BUDGET_LIMIT="100",
            PYTHONPATH=str(ROOT / "backend"),
        )
        pid = int(prior["host_pid"])
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if (
                not (Path("/proc") / str(pid) / "cmdline").exists()
                or not (Path("/proc") / str(pid) / "cmdline").read_bytes()
            ):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Old proxy did not stop; no replacement started")
        logfile = BUDGETS / f"d6-batch{index}-proxy.log"
        logfile.touch(mode=0o600)
        logfile.chmod(0o600)
        with logfile.open("ab") as log:
            proc = subprocess.Popen(
                [
                    str(ROOT / ".venv/bin/python"),
                    "-m",
                    "uvicorn",
                    "--app-dir",
                    str(ROOT / "benchmarks/d5"),
                    "proxy_observer:configured_app",
                    "--factory",
                    "--host",
                    prior["host_ip"],
                    "--port",
                    "18091",
                    "--no-access-log",
                ],
                env=env,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        for _ in range(50):
            try:
                health = httpx.get("http://localhost:18090/health", timeout=2).json()
                if health["used_slots"] == 0 and health["limit"] == 100:
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Replacement proxy not healthy; formal run paused")
        deployment = {
            **prior,
            "host_pid": proc.pid,
            "active_ledger": str(new),
            "rotated_at": now(),
        }
        DIRECT.write_text(json.dumps(deployment, indent=2))
        WORK.mkdir(exist_ok=True, parents=True)
        with (WORK / "ledger-rotations.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {"at": now(), "old": str(old), "new": str(new), "limit": 100}
                )
                + "\n"
            )
        return new
