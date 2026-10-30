"""Evaluator-only fresh-scene trials using the common bounded execution core.

This wrapper compares diagnostic systems, not the production distributed worker
or the manual console. It never gives an investigator fixture/answer mounts.
"""

import argparse
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from cloudsre.actions import classify, reconcile_and_apply
from cloudsre.durable import digest, now
from cloudsre.evidence_v3 import collect
from cloudsre.experiment_variants import VARIANTS
from cloudsre.experiment_views import KINDS, load_records
from cloudsre.kube_gateway import KubeGateway
from cloudsre.policy_v3 import authorize, parse
from cloudsre.quality import run_window

from benchmarks.d5.live import (
    ROOT,
    configure,
    exclusive,
    field,
    read,
    wait_healthy,
)

from .diagnose import trial
from .fixture import Fixture
from .protocol import PRIVATE, WORK, case_by_id
from .protocol import prepare_environment as prepare


def persist(path, value, *, private=False):
    """Atomic, durable checkpoints; private raw objects never enter the Agent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        if private:
            os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def root_state(fixture):
    """Ground truth is consulted only by this external outcome measurement."""
    change = fixture.record["changes"][fixture.record.get("root_change_index", -1)]
    current = read(change["kind"], change["name"])
    actual = field(current, change["path"])
    same_uid = current["metadata"]["uid"] == change["uid"]
    return {
        "passed": same_uid and actual == change["original"],
        "same_uid": same_uid,
        "observed_uid": current["metadata"]["uid"],
        "observed_field_sha256": digest(actual),
        "reference_field_sha256": digest(change["original"]),
        "measured_at": now(),
    }


def recovery_verdict(execution, before, after, quality, stable, ready):
    """A healthy old Pod or a naked success flag is never a recovery result."""
    required = (
        "duration_at_least_120",
        "all_scheduled_requests_recorded",
        "business_checks_at_least_12",
        "all_requests_succeeded",
        "all_invariants_succeeded",
        "monitoring_available",
        "no_new_related_severe_alert",
        "resource_samples_present",
        "p95_within_frozen_limit",
        "traffic_schedule_preserved",
    )
    checks = quality.get("checks", {})
    return bool(
        execution.get("state") == "APPLIED"
        and execution.get("http_attempted") is True
        and before.get("passed") is True
        and after.get("passed") is True
        and stable is True
        and ready is True
        and quality.get("passed") is True
        and quality.get("duration_seconds", 0) >= 120
        and quality.get("load_requests", 0) >= 120
        and quality.get("business_checks", 0) >= 12
        and all(checks.get(key) is True for key in required)
    )


def fresh_snapshot(case, fixture):
    ident = uuid.uuid4().hex
    folder = WORK / "snapshots" / ident
    folder.mkdir(parents=True)
    incident = {
        "id": uuid.uuid4().hex,
        "title": "Observed business or workload alert; investigate current evidence",
        "service": "order",
        "namespace": "sre-demo",
        "evidence_since": fixture.record["fault_at"],
    }
    persist(folder / "incident.json", incident)
    gateway = KubeGateway("read")
    try:
        raw = {
            f"sre-demo/{kind}/{name}": gateway.read(f"sre-demo/{kind}/{name}")
            for kind in ("Deployment", "Service")
            for name in ("gateway", "order", "inventory")
        }
    finally:
        gateway.close()
    persist(fixture.folder / "current-raw.json", raw, private=True)
    for kind in KINDS:
        original = os.environ.get("CLOUDSRE_LOKI_URL")
        if case.tool_failure and kind == "logs":
            os.environ["CLOUDSRE_LOKI_URL"] = "http://localhost:1"
        try:
            data = collect(kind, folder)
        except (httpx.HTTPError, RuntimeError) as exc:
            data = {
                "collection_error": {
                    "error_type": type(exc).__name__,
                    "source": "real read-only transport failure",
                }
            }
        finally:
            if case.tool_failure and kind == "logs":
                if original is None:
                    os.environ.pop("CLOUDSRE_LOKI_URL", None)
                else:
                    os.environ["CLOUDSRE_LOKI_URL"] = original
        persist(
            folder / (kind + ".json"),
            {
                "id": "e-" + uuid.uuid4().hex,
                "kind": kind,
                "collected_at": now(),
                "source": "live-read-only-tool",
                "data": data,
                "content_hash": digest(data),
            },
        )
    records = load_records(folder)
    fixture.confirm_context(records)
    return {
        "passed": True,
        "scoring_ready": True,
        "case_id": case.id,
        "split": case.split,
        "snapshot_id": ident,
        "capture_folder": str(fixture.folder),
        "fault_confirmed": True,
        "fresh_live_episode": True,
        "records_sha256": {kind: digest(record) for kind, record in records.items()},
        "incident_sha256": digest(incident),
    }


def apply_common(proposal, records, baseline, private, public):
    """Same production policy/reconciliation for every diagnostic variant.

    The caller holds the evaluator's exclusive physical experiment lock. This
    does not emulate or claim the production database lease/approval workflow.
    """
    gateway = KubeGateway("execute")
    try:
        intent = authorize(
            parse(json.dumps(proposal)),
            list(records.values()),
            gateway.read(proposal["resource"]),
            baseline,
            "sre-demo",
        )
        persist(private / "action-intent.json", intent, private=True)

        def check_active():
            if (public / "STOP").exists():
                raise RuntimeError("Evaluator stop requested")

        def dispatch(value):
            persist(
                private / "dispatch.json", {"at": now(), "intent": value}, private=True
            )

        execution = reconcile_and_apply(
            intent, gateway.read, gateway.patch, check_active, dispatch
        )
        persist(private / "execution.json", execution, private=True)
        return intent, execution
    finally:
        gateway.close()


def run(case_id, variant, repeat=0, *, diagnostic=trial):
    if variant not in VARIANTS:
        raise ValueError("Unknown diagnostic variant")
    case = case_by_id(case_id)
    configure()
    ident = uuid.uuid4().hex
    folder = WORK / "e2e" / ident
    folder.mkdir(parents=True)
    # Existing capture.prepare restart recovery finds this exact private folder.
    private = PRIVATE / "captures" / ("e2e-" + ident)
    private.mkdir(parents=True, mode=0o700)
    fixture = Fixture(case, private)
    baseline_path = ROOT / "infra/.generated/d4/baseline.json"
    baseline = json.loads(baseline_path.read_text())
    result = {
        "id": ident,
        "case_id": case_id,
        "split": case.split,
        "variant": variant,
        "repeat_index": repeat,
        "experiment_kind": "D6_live_common_executor",
        "diagnostic_replay_only": False,
        "recovery_measured": False,
        "external_recovery": False,
        "completed": False,
        "environment_valid": False,
        "expected_disposition": case.expected_disposition,
        "disposition": "not_started",
        "disposition_correct": False,
        "started_at": now(),
        "started_unix": time.time(),
        "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "execution_wrapper": "D6 common bounded executor; sandbox execution, separately validated production approval workflow",
        "model_cost_amount": None,
        "provider_http_attempts": 0,
        "known_total_tokens": 0,
    }
    intent = None
    execution = {}
    with exclusive():
        try:
            prepare()
            wait_healthy(folder)
            fixture.inject()
            result["environment_valid"] = True
            capture = fresh_snapshot(case, fixture)
            fixture.drain()
            persist(private / "capture.json", capture, private=True)
            result["snapshot_id"] = capture["snapshot_id"]
            result.update(
                provider_http_attempts=None,
                known_total_tokens=None,
                diagnostic_started=True,
            )
            persist(folder / "result.json", result)
            diagnosis = diagnostic(case_id, variant, repeat, capture_override=capture)
            result.update(
                diagnostic_run_id=diagnosis["id"],
                provider_http_attempts=diagnosis["provider_http_attempts"],
                known_total_tokens=diagnosis["known_total_tokens"],
                known_usage_attempts=diagnosis.get("known_usage_attempts"),
                unknown_usage_attempts=diagnosis.get("unknown_usage_attempts"),
                budget_remaining=diagnosis.get("budget_remaining"),
                diagnostic_protocol=diagnosis.get("protocol"),
                diagnostic_image_id=diagnosis.get("diagnostic_image_id"),
                disposition="invalid_diagnosis",
            )
            if diagnosis["snapshot_id"] != capture["snapshot_id"]:
                raise ValueError("Diagnosis belongs to a different scene")
            proposal = diagnosis.get("proposal")
            if diagnosis.get("output_valid") and proposal:
                if proposal["action"] == "escalate":
                    result["disposition"] = "escalated"
                else:
                    try:
                        intent, execution = apply_common(
                            proposal,
                            load_records(WORK / "snapshots" / capture["snapshot_id"]),
                            baseline,
                            private,
                            folder,
                        )
                        result["disposition"] = (
                            "executed"
                            if execution["state"] == "APPLIED"
                            else "execution_refused"
                        )
                    except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                        result.update(
                            disposition="blocked_by_common_executor",
                            execution_error_type=type(exc).__name__,
                        )
            result["execution"] = execution
            persist(folder / "result.json", result)
            result["recovery_readiness_passed"] = False
            if execution.get("state") == "APPLIED":
                readiness = folder / "recovery-readiness"
                readiness.mkdir()
                try:
                    wait_healthy(readiness)
                    result["recovery_readiness_passed"] = True
                except RuntimeError as exc:
                    result["readiness_error_type"] = type(exc).__name__
            result["root_before_window"] = root_state(fixture)
            # Failed plans/escalations also receive the full independent grader;
            # the fixture is NOT restored until after this result is persisted.
            quality = run_window(
                folder / "business-window",
                baseline.get("quality_by_namespace", {}).get(
                    "sre-demo", baseline["quality"]
                ),
            )
            after = root_state(fixture)
            stable = False
            if intent:
                gateway = KubeGateway("read")
                try:
                    stable = (
                        classify(intent, gateway.read(intent["resource"])) == "APPLIED"
                    )
                finally:
                    gateway.close()
            recovered = recovery_verdict(
                execution,
                result["root_before_window"],
                after,
                quality,
                stable,
                result["recovery_readiness_passed"],
            )
            result.update(
                quality=quality,
                root_after_window=after,
                root_stable=stable,
                recovery_measured=True,
                external_recovery=recovered,
                completed=True,
                disposition_correct=(
                    recovered
                    if case.expected_disposition == "recover"
                    else result["disposition"]
                    in {"escalated", "blocked_by_common_executor", "execution_refused"}
                ),
            )
            persist(folder / "pre-cleanup-result.json", result)
        except BaseException as exc:
            result.update(
                error_type=type(exc).__name__,
                interrupted=isinstance(exc, (KeyboardInterrupt, SystemExit)),
            )
            raise
        finally:
            # No cleanup observation can change external_recovery or the window.
            try:
                fields = fixture.cleanup() if fixture.record["active"] else []
                result["cleanup"] = {
                    "actor": "evaluator owned-field cleanup, not Agent repair",
                    "restored_fields": fields,
                    "passed": True,
                }
            except BaseException as exc:
                result["cleanup"] = {"passed": False, "error_type": type(exc).__name__}
                raise
            finally:
                result.update(
                    ended_at=now(), ended_unix=time.time(), folder=str(folder)
                )
                result["elapsed_seconds"] = (
                    result["ended_unix"] - result["started_unix"]
                )
                persist(folder / "result.json", result)
                print(
                    json.dumps(
                        {
                            key: result[key]
                            for key in (
                                "id",
                                "case_id",
                                "variant",
                                "completed",
                                "external_recovery",
                                "provider_http_attempts",
                            )
                        }
                    ),
                    flush=True,
                )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--repeat", type=int, default=0)
    args = parser.parse_args()
    result = run(args.case, args.variant, args.repeat)
    if not result["cleanup"]["passed"]:
        raise SystemExit("Evaluator cleanup failed; no further physical trials")


if __name__ == "__main__":
    main()
