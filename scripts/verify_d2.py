"""End-to-end Agent acceptance; the evaluator injects faults but does not repair on success."""

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
from cloudsre.business_verify import verify
from cloudsre.kube import PREFIX, deployment_snapshot, read
from cloudsre.ports import INVENTORY_URL, ORDER_URL

ROOT = Path.home() / "sre-project"
API = "http://localhost:18080"


def until(fn, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except (httpx.HTTPError, KeyError):
            pass
        time.sleep(2)
    raise RuntimeError("D2 acceptance timed out")


def main():
    runid = "d2-acceptance-" + uuid.uuid4().hex[:12]
    folder = ROOT / "evaluation/runs" / runid
    folder.mkdir(parents=True)
    report = {
        "run_id": runid,
        "checks": {},
        "verification_window_seconds": 30,
        "evaluator_cleanup_used": False,
    }
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "backend"),
        "CLOUDSRE_VERIFY_SECONDS": "30",
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
    stop = threading.Event()
    load = None
    try:
        until(lambda: "Worker ready" in (folder / "worker.log").read_text(), 45)
        assert worker.poll() is None
        report["healthy_before"] = verify(ORDER_URL, INVENTORY_URL)
        assert report["healthy_before"]["passed"]
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
        report["checks"]["second_worker_blocked_by_database_lease"] = True
        oldids = {r["id"] for r in httpx.get(API + "/incidents").json()}

        def workload():
            while not stop.is_set():
                try:
                    httpx.post(
                        ORDER_URL + "/orders",
                        json={"sku": "demo-sku", "quantity": 1},
                        headers={"Idempotency-Key": "d2-load-" + uuid.uuid4().hex},
                        timeout=5,
                    )
                except httpx.HTTPError:
                    pass
                stop.wait(1)

        load = threading.Thread(target=workload, daemon=True)
        load.start()
        wrong = "http://inventory-absent-" + uuid.uuid4().hex[:6] + ":8000"
        report["fault_value"] = wrong
        report["fault_started_at"] = time.time()
        subprocess.run(
            PREFIX
            + [
                "set",
                "env",
                "deployment/order",
                "INVENTORY_URL=" + wrong,
                "RELEASE_ID=d2-fault",
            ],
            check=True,
        )
        subprocess.run(
            PREFIX + ["rollout", "status", "deployment/order", "--timeout=120s"],
            check=True,
        )

        def find():
            for row in httpx.get(API + "/incidents").json():
                if row["id"] not in oldids and row["title"].startswith("OrderErrors:"):
                    return row

        incident = until(find, 120)
        report["incident_id"] = incident["id"]
        report["checks"]["real_alert_triggered_worker"] = True
        completed = until(
            lambda: (
                r
                if (r := httpx.get(API + "/incidents/" + incident["id"]).json())[
                    "status"
                ]
                in {"RESOLVED", "ESCALATED"}
                else None
            ),
            300,
        )
        runs = httpx.get(API + "/incidents/" + incident["id"] + "/runs").json()
        report["runs"] = runs
        report["final_status"] = completed["status"]
        assert completed["status"] == "RESOLVED", runs
        assert len(runs) == 1
        journal = runs[0]["payload"]
        assert journal["outcome"] == "agent_restored_and_verified"
        assert journal["action"]["before"]["env"]["INVENTORY_URL"] == wrong
        assert (
            journal["action"]["after"]["env"]["INVENTORY_URL"]
            == "http://inventory:8000"
        )
        assert len(journal["verification"]) >= 3
        assert all(x["result"]["passed"] for x in journal["verification"])
        phases = {x["phase"] for x in journal["events"]}
        assert {
            "INVESTIGATING",
            "PLAN_READY",
            "EXECUTING",
            "VERIFYING",
            "RESOLVED",
        } <= phases
        report["checks"]["model_proposed_and_policy_validated"] = True
        report["checks"]["agent_applied_config_repair"] = True
        report["checks"]["sustained_independent_business_checks"] = True
        report["healthy_after"] = verify(ORDER_URL, INVENTORY_URL)
        assert report["healthy_after"]["passed"]
        receipt = next(
            x
            for x in httpx.get(API + "/incidents/" + incident["id"] + "/alerts").json()
            if x["status"] == "firing"
        )
        httpx.post(
            API + "/webhooks/alertmanager", json={"alerts": [receipt["payload"]]}
        ).raise_for_status()
        time.sleep(3)
        assert (
            len(httpx.get(API + "/incidents/" + incident["id"] + "/runs").json()) == 1
        )
        report["checks"]["duplicate_delivery_did_not_rerun_repair"] = True
        report["completed_at"] = time.time()
    finally:
        # Only clean up if Agent failed to restore; never count this as Agent success.
        try:
            if (
                deployment_snapshot(read("deployment", "order"))["env"].get(
                    "INVENTORY_URL"
                )
                != "http://inventory:8000"
            ):
                report["evaluator_cleanup_used"] = True
                subprocess.run(
                    PREFIX
                    + [
                        "set",
                        "env",
                        "deployment/order",
                        "INVENTORY_URL=http://inventory:8000",
                        "RELEASE_ID=d2-evaluator-cleanup",
                    ],
                    check=True,
                )
                subprocess.run(
                    PREFIX
                    + ["rollout", "status", "deployment/order", "--timeout=120s"],
                    check=True,
                )
        finally:
            stop.set()
            if load:
                load.join(timeout=10)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait()
            log.close()
            report["note"] = (
                "Controlled single-scenario acceptance, not a success-rate benchmark. Default verification window is 120s; this acceptance used 30s. Worker stopped after test."
            )
            (folder / "verification.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
            Path("/mnt/d/uclaude/d2-last-attempt.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
    assert not report["evaluator_cleanup_used"]
    Path("/mnt/d/uclaude/d2-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                "run_id": runid,
                "incident_id": report["incident_id"],
                "checks": report["checks"],
                "tokens": report["runs"][0]["payload"]["usage"],
                "evaluator_cleanup_used": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
