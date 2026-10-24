"""Real executor/DB compensation contract, separate from autonomous repair scoring."""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from cloudsre.actions import (
    prepare_bounded_scale,
    prepare_compensation,
    reconcile_and_apply,
)
from cloudsre.domain import IncidentInput
from cloudsre.durable import DurableStore, now
from cloudsre.kube_gateway import KubeGateway
from cloudsre.quality import request, run_window
from cloudsre.store import Store
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    private = ROOT / "infra/.generated/d3"
    cfg = dotenv_values(private / "worker.env")
    # The evaluator creates the isolated event; all executor checkpoints use the runtime role.
    from cloudsre.worker import database_url

    admin = Store(database_url())
    ident = admin.create(
        IncidentInput(
            dedup_key="compensation-" + uuid.uuid4().hex,
            title="controlled compensation verification",
            service="gateway",
        )
    )[0]["id"]
    admin.engine.dispose()
    store = Store(
        cfg["DATABASE_URL"].replace("sre-infra-postgres-1:5432", "localhost:15432")
    )
    durable = DurableStore(store)
    os.environ["CLOUDSRE_EXECUTE_KUBECONFIG"] = str(private / "execute-host.yaml")
    gateway = KubeGateway("execute")
    folder = ROOT / "evaluation/runs" / ("d3-compensation-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    report = {
        "passed": False,
        "autonomous_repair": False,
        "started_at": now(),
        "evaluator_cleanup_used": False,
    }
    run = uuid.uuid4().hex
    journal = {"incident_id": ident, "run_id": run, "events": []}
    baseline = json.loads((private / "baseline.json").read_text())
    verifier_stopped = False
    initial = None
    intent = None
    try:
        with durable.acquire(ident, "sre-demo/business-graph") as lease:
            assert lease is not None
            prior = request("/verify")
            current = gateway.read("sre-demo/Deployment/gateway")
            initial = current
            intent = prepare_bounded_scale(current, 2, baseline, prior)
            lease.checkpoint(run, "NEW", "INVESTIGATING", journal)
            lease.checkpoint(run, "INVESTIGATING", "PLAN_READY", journal, intent=intent)
            lease.checkpoint(run, "PLAN_READY", "EXECUTING", journal)
            result = reconcile_and_apply(
                intent,
                gateway.read,
                gateway.patch,
                lease.check,
                lambda a: lease.checkpoint(
                    run,
                    "EXECUTING",
                    "EXECUTING",
                    journal,
                    result={"id": a["id"], "state": "DISPATCHED"},
                ),
            )
            assert result["state"] == "APPLIED"
            lease.checkpoint(run, "EXECUTING", "VERIFYING", journal, result=result)
            subprocess.run(
                ["docker", "stop", "sre-infra-verifier-api-1"],
                capture_output=True,
                check=True,
            )
            verifier_stopped = True
            try:
                request("/verify")
                raise AssertionError("Expected a real verification transport failure")
            except httpx.HTTPError as exc:
                report["verification_error"] = type(exc).__name__
            compensation = prepare_compensation(
                intent, gateway.read(intent["resource"]), prior
            )
            lease.checkpoint(
                run, "VERIFYING", "COMPENSATING", journal, intent=compensation
            )
            result = reconcile_and_apply(
                compensation,
                gateway.read,
                gateway.patch,
                lease.check,
                lambda a: lease.checkpoint(
                    run,
                    "COMPENSATING",
                    "COMPENSATING",
                    journal,
                    result={"id": a["id"], "state": "DISPATCHED"},
                ),
            )
            assert result["state"] == "APPLIED"
            lease.checkpoint(
                run, "COMPENSATING", "COMPENSATING", journal, result=result
            )
            subprocess.run(
                ["docker", "start", "sre-infra-verifier-api-1"],
                capture_output=True,
                check=True,
            )
            verifier_stopped = False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if request("/verify").get("passed"):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Verifier did not become ready after restart")
            report["quality"] = run_window(folder / "recovery", baseline["quality"])
            assert report["quality"]["passed"]
            journal.update(
                outcome="compensated_needs_review",
                ticket={
                    "status": "OPEN",
                    "reason": "Verification infrastructure was unavailable",
                },
            )
            lease.checkpoint(run, "COMPENSATING", "ESCALATED", journal)
            report.update(
                passed=True,
                incident_id=ident,
                action=intent,
                compensation=compensation,
                final_business=request("/verify"),
            )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:600]
        raise
    finally:
        if verifier_stopped:
            subprocess.run(
                ["docker", "start", "sre-infra-verifier-api-1"],
                capture_output=True,
                check=True,
            )
        if not report["passed"] and initial is not None and intent is not None:
            current = gateway.read(intent["resource"])
            if current["spec"] != initial["spec"]:
                if (
                    current["metadata"]
                    .get("annotations", {})
                    .get("cloudsre/operation-id")
                    == intent["id"]
                ):
                    gateway.patch(
                        intent["resource"],
                        [
                            {
                                "op": "test",
                                "path": "/metadata/uid",
                                "value": initial["metadata"]["uid"],
                            },
                            {
                                "op": "test",
                                "path": "/metadata/resourceVersion",
                                "value": current["metadata"]["resourceVersion"],
                            },
                            {
                                "op": "replace",
                                "path": "/spec",
                                "value": initial["spec"],
                            },
                        ],
                    )
                    report["evaluator_cleanup_used"] = True
                else:
                    report["cleanup_refused"] = (
                        "Concurrent external change; automatic overwrite refused"
                    )
        gateway.close()
        store.engine.dispose()
        report["ended_at"] = now()
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        print(
            json.dumps(
                {
                    "run_id": folder.name,
                    "passed": report["passed"],
                    "autonomous_repair": False,
                }
            )
        )


if __name__ == "__main__":
    main()
