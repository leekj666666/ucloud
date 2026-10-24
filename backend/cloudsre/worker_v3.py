"""Restartable D3 worker: database checkpoints are the authority, files are evidence."""

import json
import os
import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy.exc import SQLAlchemyError

from . import quality as quality_module
from .actions import classify, reconcile_and_apply
from .call_ledger import summarize
from .durable import DurableStore, LeaseLost, digest, now
from .kube_gateway import KubeGateway
from .policy_v3 import authorize, parse
from .quality import monitor, request, run_window
from .reporting import write_report
from .store import Store

TERMINAL = {"RESOLVED", "ESCALATED", "DIAGNOSED"}
STATE = Path(os.environ.get("CLOUDSRE_STATE_DIR", "evaluation/d3-state"))


class RetryableExecutionError(RuntimeError):
    pass


def namespace_for(store, row):
    namespaces = {
        r["payload"]["labels"].get("namespace", "sre-demo")
        for r in store.receipts(row["id"])
    }
    if len(namespaces) != 1 or not namespaces <= {"sre-demo", "sre-demo-b"}:
        raise ValueError("Missing or ambiguous namespace")
    return namespaces.pop()


def eligible(store, row):
    if row["status"] in TERMINAL or row["service"] not in {
        "gateway",
        "order",
        "inventory",
    }:
        return False
    cutoff = os.environ.get("CLOUDSRE_INCIDENT_NOT_BEFORE")
    if cutoff and row["created_at"] < cutoff:
        return False
    runs = store.runs(row["id"])
    if runs and runs[-1]["payload"].get("schema_version") != 3:
        return False
    if row["status"] != "NEW":
        return bool(runs)
    receipts = store.receipts(row["id"])
    return any(x["status"] == "firing" for x in receipts) and not any(
        x["status"] == "resolved" for x in receipts
    )


def save_file(folder, journal):
    temp = folder / "workflow.tmp"
    temp.write_text(json.dumps(journal, ensure_ascii=False, indent=2))
    temp.replace(folder / "workflow.json")


def collect_diagnoses(journal, folder):
    base = Path(os.environ["CLOUDSRE_DIAG_RESULTS"])
    all_lines = []
    for attempt in journal.get("diagnoses", []):
        source = base / attempt["id"]
        if (source / "calls.jsonl").exists():
            all_lines.extend((source / "calls.jsonl").read_text().splitlines())
        if (source / "evidence").exists():
            shutil.copytree(
                source / "evidence", folder / "evidence", dirs_exist_ok=True
            )
    (folder / "calls.jsonl").write_text(
        "\n".join(all_lines) + ("\n" if all_lines else "")
    )
    return summarize(folder / "calls.jsonl")


def process(store, row, lease, baseline, software):
    ident = row["id"]
    old = store.runs(ident)
    if old:
        journal = old[-1]["payload"]
        phase = old[-1]["phase"]
        run_id = old[-1]["id"]
    else:
        run_id = uuid.uuid4().hex
        phase = "NEW"
        journal = {
            "schema_version": 3,
            "incident_id": ident,
            "run_id": run_id,
            "started_at": now(),
            "events": [],
            "outcome": "running",
            "diagnoses": [],
            "phase_seconds": {},
            "versions": software,
        }
    folder = STATE / run_id
    folder.mkdir(parents=True, exist_ok=True)
    namespace = namespace_for(store, row)
    journal["service"] = row["service"]
    journal["namespace"] = namespace
    if namespace == "sre-demo-b":
        quality_module.NAMESPACE = namespace
        quality_module.MONITOR_JOB = "demo-b"
        quality_module.VERIFIER = os.environ["CLOUDSRE_VERIFIER_B_URL"]
    else:
        quality_module.NAMESPACE = os.environ.get("CLOUDSRE_NAMESPACE", "")
        quality_module.MONITOR_JOB = "demo"
        quality_module.VERIFIER = os.environ["CLOUDSRE_VERIFIER_URL"]
    journal["worker_instance"] = os.environ.get("HOSTNAME", "host")
    journal["lease"] = {
        "owner": lease.owner,
        "epoch": lease.epoch,
        "resource": lease.resource,
    }
    phase_start = time.monotonic()

    def checkpoint(target=None, *, intent=None, result=None, **values):
        nonlocal phase, phase_start
        target = target or phase
        journal.update(values)
        journal["phase_seconds"][phase] = (
            journal["phase_seconds"].get(phase, 0) + time.monotonic() - phase_start
        )
        phase_start = time.monotonic()
        journal["events"].append({"phase": target, "at": now(), "epoch": lease.epoch})
        lease.checkpoint(run_id, phase, target, journal, intent=intent, result=result)
        phase = target
        save_file(folder, journal)

    def barrier(name):
        # Explicit evaluator-only crash synchronization; disabled in ordinary operation.
        if os.environ.get("CLOUDSRE_TEST_PAUSE_AT") != name:
            return
        marker = STATE / ("barrier-" + ident + "-" + name + ".json")
        if marker.exists():
            return
        marker.write_text(
            json.dumps(
                {
                    "incident_id": ident,
                    "phase": name,
                    "worker_instance": journal["worker_instance"],
                    "at": now(),
                }
            )
        )
        while True:
            lease.check()
            time.sleep(0.2)

    gateway = KubeGateway("execute")
    try:
        checkpoint("INVESTIGATING" if phase == "NEW" else phase, resumed=bool(old))
        if journal.get("baseline_hash", digest(baseline)) != digest(baseline):
            raise ValueError("Trusted baseline changed during incident")
        journal["baseline_hash"] = digest(baseline)
        if phase == "RETRY_WAIT":
            retry_after = journal.get("retry_after")
            if retry_after:
                delay = (
                    datetime.fromisoformat(retry_after) - datetime.now(UTC)
                ).total_seconds()
                time.sleep(min(10, max(0, delay)))
            lease.check()
            checkpoint("INVESTIGATING", retry_after=None)
        if phase == "INVESTIGATING":
            while True:
                if (
                    datetime.now(UTC) - datetime.fromisoformat(journal["started_at"])
                ).total_seconds() > 600:
                    raise RuntimeError("Incident time budget exhausted")
                if not journal["diagnoses"] or journal["diagnoses"][-1].get(
                    "state"
                ) in {"failed", "timeout", "interrupted"}:
                    if len(journal["diagnoses"]) >= 2:
                        raise RuntimeError("Diagnostic retry budget exhausted")
                    previous = journal["diagnoses"][-1] if journal["diagnoses"] else {}
                    attempt = {"id": uuid.uuid4().hex, "state": "prepared", "at": now()}
                    if previous.get("error_kind") == "invalid_model_output":
                        attempt["feedback"] = previous["validation_error"]
                    journal["diagnoses"].append(attempt)
                    checkpoint()
                attempt = journal["diagnoses"][-1]
                endpoint = os.environ["CLOUDSRE_DIAG_URL"] + "/jobs/" + attempt["id"]
                headers = {"X-Diagnostic-Token": os.environ["CLOUDSRE_DIAG_TOKEN"]}
                incident = {
                    "id": ident,
                    "title": row["title"],
                    "service": row["service"],
                    "namespace": namespace,
                    "evidence_since": journal["started_at"],
                }
                if attempt.get("feedback"):
                    incident["previous_output_error"] = attempt["feedback"]
                barrier("before_diagnosis")
                response = httpx.post(
                    endpoint, json={"incident": incident}, headers=headers, timeout=15
                )
                response.raise_for_status()
                end = time.monotonic() + 195
                while time.monotonic() < end:
                    lease.check()
                    r = httpx.get(endpoint, headers=headers, timeout=10)
                    r.raise_for_status()
                    status = r.json()
                    if status["state"] != "running":
                        attempt.update(status)
                        checkpoint()
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Diagnostic job did not finish within budget")
                journal["usage"] = collect_diagnoses(journal, folder)
                if attempt["state"] == "complete":
                    break
                if len(journal["diagnoses"]) >= 2:
                    raise RuntimeError("Diagnostic retry budget exhausted")
                checkpoint(
                    "RETRY_WAIT",
                    resume_phase="INVESTIGATING",
                    retry_after=(datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
                )
                time.sleep(10)
                lease.check()
                checkpoint("INVESTIGATING", retry_after=None)
            source = Path(os.environ["CLOUDSRE_DIAG_RESULTS"]) / attempt["id"]
            raw = json.loads((source / "holmes-result.json").read_text())
            journal["answer"] = raw["result"]
            proposal = parse(raw["result"])
            journal["proposal"] = proposal.model_dump()
            evidence = [
                json.loads(p.read_text()) for p in (source / "evidence").glob("*.json")
            ]
            if proposal.action == "escalate":
                raise ValueError(
                    "Investigator requested escalation: " + proposal.reason[:200]
                )
            current = gateway.read(proposal.resource)
            intent = authorize(proposal, evidence, current, baseline, namespace)
            checkpoint("PLAN_READY", intent=intent, action_intent=intent)
        intent = journal["action_intent"]
        if phase == "PLAN_READY":
            barrier("after_diagnosis")
            if any(r["status"] == "resolved" for r in store.receipts(ident)):
                raise ValueError(
                    "Alert resolved before execution; stale action refused"
                )
            checkpoint("EXECUTING", intent=intent)
        if phase == "EXECUTING":

            def dispatch(action):
                checkpoint(
                    result={"id": action["id"], "state": "DISPATCHED"},
                    dispatch_count=journal.get("dispatch_count", 0) + 1,
                )
                barrier("before_write")

            def send_patch(resource, patch):
                ledger = folder / "external-actions.jsonl"
                with ledger.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "at": now(),
                                "action_id": intent["id"],
                                "event": "request_started",
                                "epoch": lease.epoch,
                                "patch_sha256": digest(patch),
                            }
                        )
                        + "\n"
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                result = gateway.patch(resource, patch)
                with ledger.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "at": now(),
                                "action_id": intent["id"],
                                "event": "response_received",
                            }
                        )
                        + "\n"
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                barrier("after_write")
                return result

            try:
                result = reconcile_and_apply(
                    intent, gateway.read, send_patch, lease.check, dispatch
                )
            except (LeaseLost, SQLAlchemyError, RetryableExecutionError):
                raise
            except (RuntimeError, OSError) as exc:
                attempts = journal.get("execution_reconciliation_errors", 0) + 1
                checkpoint(
                    execution_reconciliation_errors=attempts,
                    execution_error_type=type(exc).__name__,
                )
                if attempts >= 3:
                    raise ValueError(
                        "Execution result remains unknown after bounded readback attempts; human reconciliation required"
                    ) from exc
                raise RetryableExecutionError(
                    "Execution readback unavailable; retained intent requires reconciliation"
                ) from exc
            if result["state"] != "APPLIED":
                checkpoint(result=result, action_result=result)
                raise RuntimeError("Action rejected or conflicted; no blind repeat")
            checkpoint(
                "VERIFYING",
                result=result,
                action_result=result,
                action={
                    "before": intent["before"],
                    "after": result["after"],
                    "intent": intent,
                },
            )
        if phase == "VERIFYING":
            lease.check()
            if classify(intent, gateway.read(intent["resource"])) != "APPLIED":
                raise RuntimeError("Repair changed before recovery verification")
            end = time.monotonic() + 120
            while time.monotonic() < end:
                sample = request("/verify")
                journal.setdefault("warmup", []).append({"at": now(), "result": sample})
                checkpoint()
                monitoring_ready = False
                try:
                    readiness = monitor()
                    journal["warmup"][-1]["monitoring"] = readiness
                    monitoring_ready = True
                except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
                    journal["warmup"][-1]["monitoring_error"] = type(exc).__name__
                checkpoint()
                if sample.get("passed") and monitoring_ready:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Normal business path did not recover")
            count = journal.get("verification_attempts", 0) + 1
            checkpoint(verification_attempts=count)
            quality = run_window(
                folder / ("recovery-" + str(count)),
                baseline.get("quality_by_namespace", {}).get(
                    namespace, baseline["quality"]
                ),
            )
            journal["quality"] = quality
            lease.check()
            if not quality["passed"]:
                raise RuntimeError("Recovery quality gates failed")
            if classify(intent, gateway.read(intent["resource"])) != "APPLIED":
                raise RuntimeError("Resource drift during verification")
            journal["usage"] = collect_diagnoses(journal, folder)
            journal["outcome"] = "agent_restored_and_verified"
            report = write_report(folder, journal, software)
            checkpoint(
                "RESOLVED", report={"path": report["path"], "sha256": report["sha256"]}
            )
    except (LeaseLost, SQLAlchemyError, RetryableExecutionError):
        # Do not invent a terminal result when the authoritative database cannot commit.
        raise
    except (
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        TypeError,
        StopIteration,
        IndexError,
        httpx.HTTPError,
    ) as exc:
        journal["usage"] = collect_diagnoses(journal, folder)
        journal.update(
            outcome="needs_human",
            error_type=type(exc).__name__,
            error=str(exc)[:600],
            ticket={
                "id": "ticket-" + ident,
                "status": "OPEN",
                "reason": str(exc)[:600],
                "created_at": now(),
            },
        )
        report = write_report(folder, journal, software)
        checkpoint(
            "ESCALATED", report={"path": report["path"], "sha256": report["sha256"]}
        )
    finally:
        gateway.close()
    return journal


def main():
    store = Store(os.environ["DATABASE_URL"])
    # Migrations are a separate deployment step, not raced by worker replicas.
    durable = DurableStore(store)
    baseline = json.loads(Path(os.environ["CLOUDSRE_TRUSTED_BASELINE"]).read_text())
    software = json.loads(Path(os.environ["CLOUDSRE_VERSION_MANIFEST"]).read_text())
    STATE.mkdir(parents=True, exist_ok=True)
    print("D3 worker ready", flush=True)
    while True:
        try:
            for row in reversed(store.list()):
                if not eligible(store, row):
                    continue
                namespace = namespace_for(store, row)
                # All targets in this dependency graph share a lock; independent namespaces run concurrently.
                with durable.acquire(row["id"], namespace + "/business-graph") as lease:
                    if lease is None:
                        continue
                    current = store.get(row["id"])
                    if eligible(store, current):
                        result = process(store, current, lease, baseline, software)
                        print(
                            json.dumps(
                                {"incident_id": row["id"], "outcome": result["outcome"]}
                            ),
                            flush=True,
                        )
        except (
            SQLAlchemyError,
            LeaseLost,
            RetryableExecutionError,
            httpx.HTTPError,
            OSError,
            ValueError,
        ) as exc:
            print(
                json.dumps({"retryable_worker_error": type(exc).__name__}), flush=True
            )
        time.sleep(2)


if __name__ == "__main__":
    main()
