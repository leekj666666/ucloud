"""Single local worker: durable phases, bounded investigation, narrow execution."""

import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from dotenv import dotenv_values
from sqlalchemy import text

from .business_verify import verify
from .domain import Status
from .kube import deployment_snapshot, guarded_patch, inventory_snapshot, read
from .policy import parse_proposal, validate_plan
from .ports import INVENTORY_URL, ORDER_URL, business_ports
from .store import Store

ROOT = Path(__file__).resolve().parents[2]


def now():
    return datetime.now(UTC).isoformat()


def database_url():
    cfg = dotenv_values(ROOT / "infra/.env")
    return (
        "postgresql+psycopg://sre:"
        + quote(cfg["POSTGRES_PASSWORD"], safe="")
        + "@localhost:15432/sre"
    )


def desired_url():
    for doc in yaml.safe_load_all((ROOT / "deploy/d1-kubernetes.yaml").read_text()):
        if doc and doc["kind"] == "Deployment" and doc["metadata"]["name"] == "order":
            return next(
                e["value"]
                for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]
                if e["name"] == "INVENTORY_URL"
            )
    raise RuntimeError("Trusted baseline missing")


def eligible(store, row):
    receipts = store.receipts(row["id"])
    return (
        row["status"] == Status.NEW
        and row["service"] == "order"
        and any(r["status"] == "firing" for r in receipts)
        and not any(r["status"] == "resolved" for r in receipts)
    )


def run_incident(store, row, lease_check=lambda: None):
    ident = row["id"]
    runid = uuid.uuid4().hex
    folder = ROOT / "evaluation/runs" / ("d2-" + runid)
    folder.mkdir(parents=True)
    (folder / "incident.json").write_text(
        json.dumps({"id": ident, "title": row["title"], "service": row["service"]})
    )
    state = Status.NEW
    journal = {
        "incident_id": ident,
        "run_id": runid,
        "events": [],
        "outcome": "running",
        "verification": [],
    }

    def checkpoint(target, **values):
        nonlocal state
        if state != target:
            store.transition(ident, state, target, diagnosis=journal.get("answer"))
            state = target
        journal.update(values)
        journal["events"].append({"phase": state, "at": now()})
        store.save_run(runid, ident, state, journal)
        (folder / "workflow.json").write_text(
            json.dumps(journal, ensure_ascii=False, indent=2)
        )

    # Competing claim failures stop here without modifying another worker's run.
    store.transition(ident, Status.NEW, Status.INVESTIGATING)
    state = Status.INVESTIGATING
    checkpoint(state)
    try:
        env = {**os.environ, "CLOUDSRE_RUN_DIR": str(folder)}
        key = dotenv_values("/mnt/d/uclaude/model.env").get("DEEPSEEK_API_KEY", "")
        p = subprocess.run(
            [
                str(ROOT / ".venv-holmes/bin/python"),
                str(ROOT / "diagnosis/incident_runner.py"),
                str(folder),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
        output = p.stdout + "\n" + p.stderr
        if key:
            output = output.replace(key, "[REDACTED]")
        (folder / "diagnosis-process.log").write_text(output)
        if p.returncode != 0:
            raise RuntimeError("Diagnosis process failed; inspect redacted log")
        raw = json.loads((folder / "holmes-result.json").read_text())
        journal["usage"] = {
            k: raw.get(k)
            for k in [
                "total_tokens",
                "prompt_tokens",
                "completion_tokens",
                "num_llm_calls",
            ]
        }
        journal["answer"] = raw["result"]
        proposal = parse_proposal(raw["result"])
        journal["proposal"] = proposal.model_dump()
        evidence = [
            json.loads(p.read_text()) for p in (folder / "evidence").glob("*.json")
        ]
        current = deployment_snapshot(read("deployment", "order"))
        allowed = validate_plan(
            proposal, evidence, current, desired_url(), inventory_snapshot()
        )
        if any(r["status"] == "resolved" for r in store.receipts(ident)):
            raise ValueError("Alert already resolved; stale repair refused")
        checkpoint(Status.PLAN_READY, policy=allowed)
        lease_check()
        checkpoint(
            Status.EXECUTING,
            action={"state": "intent_recorded", "before": current, "policy": allowed},
        )
        try:
            after = guarded_patch(current, allowed["old"], allowed["new"], ident)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            # Reconcile an ambiguous outcome without repeating the mutation.
            after = deployment_snapshot(read("deployment", "order"))
            if (
                after["env"].get("INVENTORY_URL") != allowed["new"]
                or after["env"].get("RELEASE_ID") != "d2-repair-" + ident[:8]
            ):
                raise RuntimeError(
                    "Action outcome unknown or not applied; no automatic retry"
                ) from None
        checkpoint(
            Status.VERIFYING,
            action={
                "state": "applied",
                "before": current,
                "after": after,
                "policy": allowed,
            },
        )
        target_release = "d2-repair-" + ident[:8]
        for attempt in range(45):
            try:
                if (
                    httpx.get(ORDER_URL + "/health", timeout=2).headers.get(
                        "x-release-id"
                    )
                    == target_release
                ):
                    break
            except httpx.HTTPError:
                pass
            if attempt == 44:
                raise RuntimeError("Port forward did not reconnect to repaired release")
            time.sleep(1)
        window = int(os.environ.get("CLOUDSRE_VERIFY_SECONDS", "120"))
        if not 30 <= window <= 300:
            raise ValueError("Verification window must be 30..300 seconds")
        started = time.monotonic()
        while True:
            sample = verify(ORDER_URL, INVENTORY_URL)
            journal["verification"].append({"at": now(), "result": sample})
            checkpoint(state)
            if not sample["passed"]:
                raise RuntimeError(
                    "Business verification failed after repair; needs investigation"
                )
            if time.monotonic() - started >= window:
                break
            time.sleep(max(0, min(10, window - (time.monotonic() - started))))
        final = deployment_snapshot(read("deployment", "order"))
        if final["uid"] != after["uid"] or final["env"] != after["env"]:
            raise RuntimeError("Configuration changed during verification")
        checkpoint(
            Status.RESOLVED,
            outcome="agent_restored_and_verified",
            verification_window_seconds=window,
        )
    except (
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        OSError,
        KeyError,
        TypeError,
    ) as exc:
        # Preserve improved state on uncertain failure; do not blindly reapply a bad config.
        checkpoint(
            Status.ESCALATED,
            outcome="needs_human",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
    print(
        json.dumps({"incident_id": ident, "status": state, "run_id": runid}), flush=True
    )
    return journal


def main():
    store = Store(database_url())
    store.initialize()
    with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        if not lock.execute(text("SELECT pg_try_advisory_lock(6281037)")).scalar():
            raise RuntimeError("Another local worker already owns the executor lease")

        def lease_check():
            if not lock.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND objid=6281037 AND granted)"
                )
            ).scalar():
                raise RuntimeError("Executor lease lost")

        with business_ports():
            print(
                "Worker ready; automatic policy limited to sre-demo/order INVENTORY_URL",
                flush=True,
            )
            while True:
                lease_check()
                for row in reversed(store.list()):
                    if eligible(store, row):
                        run_incident(store, row, lease_check)
                time.sleep(2)


if __name__ == "__main__":
    main()
