"""Bounded diagnosis, version-bound mutation and independent recovery verification."""

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

from .call_ledger import summarize
from .domain import Status
from .kube import deployment_snapshot, guarded_patch, inventory_snapshot, read
from .policy import parse_proposal, validate_plan
from .quality import request, run_window
from .reporting import versions, write_report
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
    cutoff = os.environ.get("CLOUDSRE_INCIDENT_NOT_BEFORE")
    return (
        row["status"] == Status.NEW
        and row["service"] == "order"
        and (not cutoff or row["created_at"] >= cutoff)
        and any(r["status"] == "firing" for r in receipts)
        and not any(r["status"] == "resolved" for r in receipts)
    )


def run_incident(store, row, lease_check=lambda: None):
    ident, runid = row["id"], uuid.uuid4().hex
    folder = ROOT / "evaluation/runs" / ("d2-" + runid)
    folder.mkdir(parents=True)
    (folder / "incident.json").write_text(
        json.dumps({k: row[k] for k in ("id", "title", "service")})
    )
    software = versions(ROOT)
    state, phase_start = Status.NEW, time.monotonic()
    journal = {
        "incident_id": ident,
        "run_id": runid,
        "events": [],
        "phase_seconds": {},
        "outcome": "running",
        "verification": [],
        "versions": software,
    }

    def persist():
        store.save_run(runid, ident, state, journal)
        temp = folder / "workflow.tmp"
        temp.write_text(json.dumps(journal, ensure_ascii=False, indent=2))
        temp.replace(folder / "workflow.json")

    def checkpoint(target, **values):
        nonlocal state, phase_start
        elapsed = time.monotonic() - phase_start
        journal["phase_seconds"][str(state)] = (
            journal["phase_seconds"].get(str(state), 0) + elapsed
        )
        phase_start = time.monotonic()
        if state != target:
            store.transition(ident, state, target, diagnosis=journal.get("answer"))
            state = target
        journal.update(values)
        journal["events"].append({"phase": state, "at": now()})
        persist()

    store.transition(ident, Status.NEW, Status.INVESTIGATING)
    state = Status.INVESTIGATING
    checkpoint(state)
    try:
        baseline_path = Path(os.environ["CLOUDSRE_HEALTHY_BASELINE"])
        baseline = json.loads(baseline_path.read_text())
        if not baseline.get("passed") or baseline["duration_seconds"] < 120:
            raise ValueError("Verified same-load healthy baseline required")
        journal["baseline"] = {
            "path": str(baseline_path),
            "p95_seconds": baseline["p95_seconds"],
            "samples_sha256": baseline["samples_sha256"],
        }
        env = {**os.environ, "CLOUDSRE_RUN_DIR": str(folder)}
        key = dotenv_values("/mnt/d/uclaude/model.env").get("DEEPSEEK_API_KEY", "")
        try:
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
        except subprocess.TimeoutExpired as exc:
            output = str(exc.stdout or "") + "\n" + str(exc.stderr or "")
            if key:
                output = output.replace(key, "[REDACTED]")
            (folder / "diagnosis-process.log").write_text(output)
            raise RuntimeError(
                "Diagnosis timed out; started HTTP attempts retain unknown usage"
            ) from None
        if key:
            output = output.replace(key, "[REDACTED]")
        (folder / "diagnosis-process.log").write_text(output)
        journal["usage"] = summarize(folder / "calls.jsonl")
        if p.returncode != 0:
            raise RuntimeError("Diagnosis process failed; inspect redacted log")
        raw = json.loads((folder / "holmes-result.json").read_text())
        if journal["usage"]["http_attempts"] < int(raw.get("num_llm_calls") or 1):
            raise RuntimeError("Model HTTP accounting incomplete; action refused")
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
        after = guarded_patch(current, allowed["old"], allowed["new"], ident)
        checkpoint(
            Status.VERIFYING,
            action={
                "state": "applied",
                "before": current,
                "after": after,
                "policy": allowed,
            },
        )
        journal["warmup"] = []
        end = time.monotonic() + 45
        while time.monotonic() < end:
            sample = request("/verify")
            journal["warmup"].append({"at": now(), "result": sample})
            persist()
            if sample.get("passed"):
                break
            time.sleep(2)
        else:
            raise RuntimeError("Normal-entry business path did not recover")
        quality = run_window(folder / "recovery-quality", baseline)
        journal["quality"] = quality
        if not quality["passed"]:
            raise RuntimeError("Recovery quality gates failed")
        final = deployment_snapshot(read("deployment", "order"))
        if any(final[k] != after[k] for k in ("uid", "generation", "spec_hash")):
            raise RuntimeError("Deployment changed during verification")
        checkpoint(
            Status.RESOLVED,
            outcome="agent_restored_and_verified",
            verification_window_seconds=120,
        )
    except (
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        TypeError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        checkpoint(
            Status.ESCALATED,
            outcome="needs_human",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
    finally:
        journal["usage"] = summarize(folder / "calls.jsonl")
        report = write_report(folder, journal, software)
        journal["report"] = {"path": report["path"], "sha256": report["sha256"]}
        persist()
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

        print(
            "Worker ready; automatic policy limited to sre-demo/order INVENTORY_URL",
            flush=True,
        )
        handled = 0
        while True:
            lease_check()
            for row in reversed(store.list()):
                if eligible(store, row):
                    run_incident(store, row, lease_check)
                    handled += 1
                    if handled >= int(
                        os.environ.get("CLOUDSRE_MAX_INCIDENTS", "1000000")
                    ):
                        return
            time.sleep(2)


if __name__ == "__main__":
    main()
