"""Frozen D2 acceptance: neutral setup, real alert, no evaluator repair on success."""

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cloudsre.kube import PREFIX, deployment_snapshot, read
from cloudsre.quality import alert_keys, monitor, request, run_window
from cloudsre.reporting import versions

ROOT = Path(__file__).resolve().parents[1]
API = "http://localhost:18080"


def now():
    return datetime.now(UTC).isoformat()


def command(*args):
    return subprocess.check_output(PREFIX + list(args), text=True)


def wait(fn, seconds=120):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except (httpx.HTTPError, ValueError, KeyError, RuntimeError):
            pass
        time.sleep(1)
    raise RuntimeError("D2 acceptance condition timed out")


def incidents():
    response = httpx.get(API + "/incidents", timeout=10)
    response.raise_for_status()
    return response.json()


def runs(ident):
    response = httpx.get(API + "/incidents/" + ident + "/runs", timeout=10)
    response.raise_for_status()
    return response.json()


def main():
    folder = ROOT / "evaluation/runs" / ("d2-complete-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    report = {
        "run_id": folder.name,
        "started_at": now(),
        "checks": {},
        "evaluator_cleanup_used": False,
        "software": versions(ROOT),
        "configuration": {
            "window_seconds": 120,
            "requests_per_second": 1,
            "minimum_business_checks": 12,
            "max_errors": 0,
            "p95_limit": "max(healthy_p95 * 2, 1 second)",
        },
    }
    (folder / "frozen-config.json").write_text(json.dumps(report, indent=2))
    worker, log, load = None, None, None
    stop = threading.Event()
    fault_records = []
    injected = False

    def persist():
        (folder / "acceptance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        Path("/mnt/d/uclaude/d2-complete-last-attempt.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )

    try:
        since = now()
        # This healthy setup happens before baseline and is not an Agent repair.
        command(
            "set", "env", "deployment/order", "RELEASE_ID=r-" + uuid.uuid4().hex[:12]
        )
        command("rollout", "status", "deployment/order", "--timeout=120s")
        wait(lambda: request("/verify").get("passed"))
        wait(lambda: not alert_keys(monitor()), 150)
        print("Healthy baseline 120s started", flush=True)
        baseline = run_window(folder / "healthy-baseline")
        report["healthy_baseline"] = baseline
        persist()
        assert baseline["passed"], baseline
        report["checks"]["healthy_baseline_same_load_120s"] = True
        cutoff = now()
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend"),
            "CLOUDSRE_HEALTHY_BASELINE": str(folder / "healthy-baseline/result.json"),
            "CLOUDSRE_EVIDENCE_SINCE": since,
            "CLOUDSRE_INCIDENT_NOT_BEFORE": cutoff,
            "CLOUDSRE_MAX_INCIDENTS": "2",
        }
        log = (folder / "worker.log").open("w")
        worker = subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "-m", "cloudsre.worker"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait(lambda: "Worker ready" in (folder / "worker.log").read_text(), 30)
        other = subprocess.run(
            [str(ROOT / ".venv/bin/python"), "-m", "cloudsre.worker"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert other.returncode != 0 and "already owns" in other.stderr
        report["checks"]["second_worker_excluded"] = True
        previous = {r["id"] for r in incidents()}
        wrong = "http://inventory-api-" + uuid.uuid4().hex[:8] + ":8000"
        report["fault"] = {
            "value": wrong,
            "at": now(),
            "release": "r-" + uuid.uuid4().hex[:12],
        }
        injected = True
        command(
            "set",
            "env",
            "deployment/order",
            "INVENTORY_URL=" + wrong,
            "RELEASE_ID=" + report["fault"]["release"],
        )
        command("rollout", "status", "deployment/order", "--timeout=120s")

        def workload():
            with (folder / "fault-traffic.jsonl").open("w") as stream:
                while not stop.is_set():
                    entry = {"started_at": now()}
                    try:
                        entry["result"] = request("/sample")
                    except (httpx.HTTPError, ValueError) as exc:
                        entry["error_type"] = type(exc).__name__
                    entry["completed_at"] = now()
                    fault_records.append(entry)
                    stream.write(json.dumps(entry) + "\n")
                    stream.flush()
                    stop.wait(1)

        load = threading.Thread(target=workload, daemon=True)
        load.start()
        print("Neutral configuration incident injected; waiting for Agent", flush=True)

        def selected():
            for row in incidents():
                if row["id"] not in previous and row["service"] == "order":
                    found = runs(row["id"])
                    if found:
                        return row

        incident = wait(selected, 150)
        ident = incident["id"]
        report["incident_id"] = ident
        receipts = httpx.get(API + "/incidents/" + ident + "/alerts", timeout=10).json()
        assert any(x["status"] == "firing" for x in receipts)
        report["checks"]["real_alert_started_dynamic_investigation"] = True
        end = time.monotonic() + 420
        journal = None
        while time.monotonic() < end:
            records = runs(ident)
            if records:
                candidate = records[-1]["payload"]
                phases = [e["phase"] for e in candidate["events"]]
                if "EXECUTING" in phases:
                    stop.set()
                if candidate.get("report") and candidate["outcome"] != "running":
                    journal = candidate
                    break
            time.sleep(1)
        if load:
            stop.set()
            load.join(timeout=40)
        assert journal is not None, "Worker did not finish within bounded acceptance"
        report["journal"] = journal
        persist()
        assert journal["outcome"] == "agent_restored_and_verified", journal.get("error")
        assert len(runs(ident)) == 1
        assert journal["action"]["before"]["env"]["INVENTORY_URL"] == wrong
        assert (
            journal["action"]["after"]["env"]["INVENTORY_URL"]
            == "http://inventory:8000"
        )
        assert (
            journal["quality"]["passed"]
            and journal["quality"]["duration_seconds"] >= 120
        )
        assert journal["quality"]["business_checks"] >= 12
        assert journal["quality"]["load_requests"] >= 120
        assert (
            journal["usage"]["http_attempts"] >= 1
            and journal["usage"]["known_usage_calls"] >= 1
        )
        assert journal["usage"]["cost_amount"] is None
        report["checks"].update(
            {
                "agent_applied_repair": True,
                "recovery_quality_120s": True,
                "call_by_call_usage_retained": True,
                "structured_incident_report": True,
            }
        )
        report["fault_traffic"] = {
            "samples": len(fault_records),
            "path": str(folder / "fault-traffic.jsonl"),
        }
        assert not load.is_alive()
        if fault_records:
            assert (
                max(r["completed_at"] for r in fault_records)
                <= journal["quality"]["started_at"]
            ), "Fault workload overlapped formal recovery window"
        report["checks"]["recovery_load_matches_healthy_baseline"] = True
        report["final_business"] = request("/verify")
        assert report["final_business"]["passed"]
        assert worker.poll() is None, "Duplicate acceptance requires a live worker"
        firing = next(x for x in receipts if x["status"] == "firing")
        httpx.post(
            API + "/webhooks/alertmanager",
            json={"alerts": [firing["payload"]]},
            timeout=10,
        ).raise_for_status()
        time.sleep(5)
        assert worker.poll() is None, "Worker stopped during duplicate acceptance"
        assert len(runs(ident)) == 1
        report["checks"]["duplicate_alert_did_not_repeat_action"] = True
        report["passed"] = True
    except (
        AssertionError,
        RuntimeError,
        ValueError,
        OSError,
        KeyError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        report.update(
            passed=False, error_type=type(exc).__name__, error=str(exc)[:1500]
        )
        raise
    finally:
        stop.set()
        if load:
            load.join(timeout=40)
        if worker and worker.poll() is None:
            os.killpg(worker.pid, signal.SIGTERM)
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait()
        if log:
            log.close()
        if (
            injected
            and deployment_snapshot(read("deployment", "order"))["env"].get(
                "INVENTORY_URL"
            )
            != "http://inventory:8000"
        ):
            report["evaluator_cleanup_used"] = True
            command(
                "set",
                "env",
                "deployment/order",
                "INVENTORY_URL=http://inventory:8000",
                "RELEASE_ID=r-" + uuid.uuid4().hex[:12],
            )
            command("rollout", "status", "deployment/order", "--timeout=120s")
        report["ended_at"] = now()
        if report["evaluator_cleanup_used"]:
            report["passed"] = False
        persist()
        print(
            json.dumps(
                {
                    "run_id": folder.name,
                    "passed": report.get("passed", False),
                    "cleanup": report["evaluator_cleanup_used"],
                    "error": report.get("error"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
