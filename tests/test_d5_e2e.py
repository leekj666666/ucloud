import contextlib
import json
from types import SimpleNamespace

import pytest

from benchmarks.d5 import e2e
from benchmarks.d5.catalog import DEV


def complete_quality():
    return {
        "passed": True,
        "duration_seconds": 120,
        "load_requests": 120,
        "business_checks": 12,
        "checks": {
            key: True
            for key in (
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
        },
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "unfixed_root",
        "recreated_root",
        "no_action",
        "drift",
        "short_window",
        "partial_load",
        "partial_invariants",
        "missing_checks",
        "monitoring_unavailable",
        "readiness_failed",
    ],
)
def test_d5_recovery_cannot_be_inferred_from_healthy_business_alone(mutation):
    execution = {"state": "APPLIED", "http_attempted": True}
    before = {"passed": True}
    after = {"passed": True}
    quality = complete_quality()
    stable = True
    ready = True
    assert e2e.recovery_verdict(execution, before, after, quality, stable, ready)
    if mutation == "unfixed_root":
        before["passed"] = False
    elif mutation == "recreated_root":
        after["passed"] = False
    elif mutation == "no_action":
        execution["http_attempted"] = False
    elif mutation == "drift":
        stable = False
    elif mutation == "short_window":
        quality["duration_seconds"] = 119.9
    elif mutation == "partial_load":
        quality["load_requests"] = 119
    elif mutation == "partial_invariants":
        quality["business_checks"] = 11
    elif mutation == "missing_checks":
        quality["checks"].pop("resource_samples_present")
    elif mutation == "monitoring_unavailable":
        quality["checks"]["monitoring_available"] = False
    elif mutation == "readiness_failed":
        ready = False
    assert not e2e.recovery_verdict(execution, before, after, quality, stable, ready)


def test_d5_root_measurement_is_uid_bound_even_if_field_matches(monkeypatch):
    fixture = SimpleNamespace(
        record={
            "changes": [
                {
                    "kind": "deployment",
                    "name": "order",
                    "path": "/spec/replicas",
                    "uid": "original",
                    "original": 1,
                }
            ]
        }
    )
    monkeypatch.setattr(
        e2e,
        "read",
        lambda *args: {"metadata": {"uid": "replacement"}, "spec": {"replicas": 1}},
    )
    measured = e2e.root_state(fixture)
    assert not measured["same_uid"] and not measured["passed"]
    assert measured["observed_field_sha256"] == measured["reference_field_sha256"]


def setup_episode(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    baseline = root / "infra/.generated/d4/baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"quality": {"p95_seconds": 0.05}}))
    monkeypatch.setattr(e2e, "ROOT", root)
    monkeypatch.setattr(e2e, "WORK", root / "runtime")
    monkeypatch.setattr(e2e, "PRIVATE", root / "private")
    monkeypatch.setattr(e2e, "configure", lambda: None)
    monkeypatch.setattr(e2e, "prepare", lambda: None)
    monkeypatch.setattr(e2e, "exclusive", contextlib.nullcontext)
    monkeypatch.setattr(e2e, "wait_healthy", lambda folder: None)
    monkeypatch.setattr(e2e, "load_records", lambda folder: {})
    calls = {"cleaned": False, "repaired": False, "grader_calls": 0}

    class Fixture:
        def __init__(self, case, folder):
            self.case = case
            self.folder = folder
            self.record = {"active": False}

        def inject(self):
            self.record["active"] = True

        def drain(self):
            pass

        def cleanup(self):
            calls.update(cleaned=True, repaired=True)
            return ["evaluator-reference-field"]

    monkeypatch.setattr(e2e, "Fixture", Fixture)
    monkeypatch.setattr(
        e2e,
        "fresh_snapshot",
        lambda case, fixture: {
            "snapshot_id": "fresh-episode",
            "capture_folder": str(fixture.folder),
            "passed": True,
            "scoring_ready": True,
        },
    )
    monkeypatch.setattr(
        e2e, "root_state", lambda fixture: {"passed": calls["repaired"]}
    )

    def grader(folder, baseline):
        assert not calls["cleaned"], (
            "Evaluator cleanup must occur after outcome measurement"
        )
        assert baseline == {"p95_seconds": 0.05}
        calls["grader_calls"] += 1
        return complete_quality()

    monkeypatch.setattr(e2e, "run_window", grader)

    class Gateway:
        def __init__(self, role):
            assert role == "read"

        def read(self, resource):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(e2e, "KubeGateway", Gateway)
    monkeypatch.setattr(e2e, "classify", lambda intent, raw: "APPLIED")
    return calls, root


@pytest.mark.parametrize("escalate", [False, True])
def test_d5_live_episode_uses_fresh_scene_and_cleanup_does_not_change_result(
    monkeypatch, tmp_path, escalate
):
    calls, root = setup_episode(monkeypatch, tmp_path)

    def diagnostic(case, variant, repeat, *, capture_override):
        assert case == "dev_01" and variant == "B0"
        assert capture_override["snapshot_id"] == "fresh-episode"
        assert not calls["cleaned"]
        return {
            "id": "diag-id",
            "snapshot_id": "fresh-episode",
            "provider_http_attempts": 0,
            "known_total_tokens": 0,
            "output_valid": True,
            "proposal": {"action": "escalate" if escalate else "restore_env"},
        }

    def apply(*args):
        calls["repaired"] = True
        return {"resource": "sre-demo/Deployment/order"}, {
            "state": "APPLIED",
            "http_attempted": True,
        }

    monkeypatch.setattr(e2e, "apply_common", apply)
    result = e2e.run("dev_01", "B0", diagnostic=diagnostic)
    assert calls["grader_calls"] == 1 and calls["cleaned"]
    assert result["external_recovery"] is (not escalate)
    assert result["recovery_measured"] and result["completed"]
    assert result["cleanup"]["actor"].startswith("evaluator")
    files = list((root / "runtime/e2e").glob("*/pre-cleanup-result.json"))
    before_cleanup = json.loads(files[0].read_text())
    assert before_cleanup["external_recovery"] == result["external_recovery"]
    assert "cleanup" not in before_cleanup


def test_d5_scene_mismatch_refuses_execution_and_preserves_real_usage(
    monkeypatch, tmp_path
):
    calls, root = setup_episode(monkeypatch, tmp_path)
    monkeypatch.setattr(
        e2e, "apply_common", lambda *args: pytest.fail("Stale scene must not execute")
    )

    def diagnostic(*args, **kwargs):
        return {
            "id": "stale",
            "snapshot_id": "different-episode",
            "provider_http_attempts": 3,
            "known_total_tokens": 100,
            "output_valid": True,
            "proposal": {"action": "restore_env"},
        }

    with pytest.raises(ValueError, match="different scene"):
        e2e.run("dev_01", "B3", diagnostic=diagnostic)
    result = json.loads(next((root / "runtime/e2e").glob("*/result.json")).read_text())
    assert result["provider_http_attempts"] == 3
    assert not result["external_recovery"] and not result["completed"]
    assert calls["cleaned"] and calls["grader_calls"] == 0


def test_d5_interrupted_diagnostic_does_not_report_unknown_usage_as_zero(
    monkeypatch, tmp_path
):
    calls, root = setup_episode(monkeypatch, tmp_path)

    def diagnostic(*args, **kwargs):
        raise RuntimeError("Interrupted after upstream dispatch")

    with pytest.raises(RuntimeError, match="Interrupted"):
        e2e.run("dev_01", "B3", diagnostic=diagnostic)
    result = json.loads(next((root / "runtime/e2e").glob("*/result.json")).read_text())
    assert result["diagnostic_started"]
    assert result["provider_http_attempts"] is None
    assert result["known_total_tokens"] is None
    assert calls["cleaned"] and not result["external_recovery"]


def test_d5_fresh_capture_hashes_public_observations_without_exposing_private_objects(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(e2e, "WORK", tmp_path / "runtime")
    private = tmp_path / "private-fixture"
    private.mkdir(mode=0o700)
    fixture = SimpleNamespace(
        folder=private, record={"fault_at": "2026-10-25T00:00:00Z"}
    )

    class Gateway:
        def __init__(self, role):
            assert role == "read"

        def read(self, key):
            return {
                "private_only_marker": "evaluator-raw-not-an-agent-tool",
                "key": key,
            }

        def close(self):
            pass

    monkeypatch.setattr(e2e, "KubeGateway", Gateway)
    monkeypatch.setattr(e2e, "collect", lambda kind, folder: {"observed": kind})
    capture = e2e.fresh_snapshot(DEV[0], fixture)
    snapshot = e2e.WORK / "snapshots" / capture["snapshot_id"]
    records = e2e.load_records(snapshot)
    incident = json.loads((snapshot / "incident.json").read_text())
    assert capture["records_sha256"] == {
        kind: e2e.digest(record) for kind, record in records.items()
    }
    assert capture["incident_sha256"] == e2e.digest(incident)
    assert {path.name for path in snapshot.iterdir()} == {
        "incident.json",
        *(kind + ".json" for kind in e2e.KINDS),
    }
    assert "expected_disposition" not in incident and "case_id" not in incident
    assert all(
        "private_only_marker" not in path.read_text() for path in snapshot.iterdir()
    )
    raw = private / "current-raw.json"
    assert "private_only_marker" in raw.read_text()
    assert raw.stat().st_mode & 0o777 == 0o600
