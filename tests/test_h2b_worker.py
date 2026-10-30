import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from cloudsre import worker_v3
from cloudsre.domain import Status, check_transition
from cloudsre.durable import now
from cloudsre.h2b_feedback import BUSINESS_CHECKS, REQUIRED_SAFETY_CHECKS


class FakeLease:
    owner = "worker"
    epoch = 1
    resource = "sre-demo/business-graph"

    def __init__(self):
        self.events = []

    def check(self):
        pass

    def checkpoint(self, run_id, expected, target, payload, **kwargs):
        if expected != target:
            check_transition(Status(expected), Status(target))
        self.events.append((target, copy.deepcopy(payload)))


class FakeControl:
    def __init__(self, store):
        pass

    def check(self, ident):
        return {"execution_mode": "manual"}

    def get(self, ident):
        return {"namespace": "sre-demo", "execution_mode": "manual"}

    def checkpoint_guard(self, ident):
        return nullcontext()

    def decision(self, action_id):
        return None


class FakeGateway:
    def __init__(self, mode):
        pass

    def read(self, resource):
        return {}

    def close(self):
        pass


@pytest.fixture
def setup_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDSRE_OPERATOR_CONTROL", "1")
    monkeypatch.setenv("CLOUDSRE_VERIFIER_URL", "http://unused")
    monkeypatch.setenv("CLOUDSRE_DIAG_RESULTS", str(tmp_path))
    monkeypatch.setattr(worker_v3, "STATE", tmp_path)
    monkeypatch.setattr(worker_v3, "Controls", FakeControl)
    monkeypatch.setattr(worker_v3, "KubeGateway", FakeGateway)
    monkeypatch.setattr(worker_v3, "classify", lambda intent, current: "APPLIED")
    monkeypatch.setattr(worker_v3, "request", lambda path: {"passed": True})
    monkeypatch.setattr(worker_v3, "monitor", dict)
    monkeypatch.setattr(
        worker_v3.quality_module,
        "RecoveryReadiness",
        lambda: SimpleNamespace(observe=lambda *args: True),
    )
    monkeypatch.setattr(
        worker_v3, "write_report", lambda *args: {"path": "report", "sha256": "hash"}
    )
    checks = {key: True for key in BUSINESS_CHECKS | REQUIRED_SAFETY_CHECKS}
    checks["all_invariants_succeeded"] = False
    monkeypatch.setattr(
        worker_v3,
        "run_window",
        lambda *args, **kwargs: {"passed": False, "checks": checks},
    )
    intent = {
        "id": "old-action",
        "resource": "sre-demo/Deployment/order",
        "before": {},
        "after": {},
    }
    journal = {
        "schema_version": 3,
        "incident_id": "case",
        "run_id": "run",
        "started_at": now(),
        "events": [],
        "outcome": "running",
        "diagnoses": [{"id": "prior", "state": "complete"}],
        "phase_seconds": {},
        "action_intent": intent,
        "action_result": {"state": "APPLIED"},
        "proposal": {
            "action": "restore_env",
            "resource": intent["resource"],
            "value": "old",
        },
    }
    store = SimpleNamespace(
        runs=lambda ident: [{"payload": journal, "phase": "VERIFYING", "id": "run"}]
    )
    row = {"id": "case", "service": "order", "title": "test"}
    return store, row, FakeLease(), {"quality": {}}, {}


def test_verified_business_failure_creates_one_fresh_diagnosis(
    setup_worker, monkeypatch
):
    monkeypatch.setenv("CLOUDSRE_H2B_ENABLED", "1")
    store, row, lease, baseline, software = setup_worker
    result = worker_v3.process(store, row, lease, baseline, software)
    assert lease.events[-1][0] == "INVESTIGATING"
    assert result["h2b_rounds"] == 1
    assert result["diagnoses"][-1]["state"] == "prepared"
    assert result["diagnoses"][-1]["id"] != "prior"
    assert result["h2b_history"][0]["action_intent"]["id"] == "old-action"
    assert result["h2b_feedback"]["failed_checks"] == ["all_invariants_succeeded"]


def test_default_off_escalates_without_reinvestigation(setup_worker, monkeypatch):
    monkeypatch.delenv("CLOUDSRE_H2B_ENABLED", raising=False)
    store, row, lease, baseline, software = setup_worker
    result = worker_v3.process(store, row, lease, baseline, software)
    assert lease.events[-1][0] == "ESCALATED"
    assert result["outcome"] == "needs_human"
    assert len(result["diagnoses"]) == 1


def test_second_failed_round_escalates(setup_worker, monkeypatch):
    monkeypatch.setenv("CLOUDSRE_H2B_ENABLED", "1")
    store, row, lease, baseline, software = setup_worker
    store.runs(row["id"])[0]["payload"]["h2b_rounds"] = 1
    result = worker_v3.process(store, row, lease, baseline, software)
    assert lease.events[-1][0] == "ESCALATED"
    assert len(result["diagnoses"]) == 1


def test_resource_drift_escalates_without_new_diagnosis(setup_worker, monkeypatch):
    monkeypatch.setenv("CLOUDSRE_H2B_ENABLED", "1")
    monkeypatch.setattr(worker_v3, "classify", lambda intent, current: "CONFLICT")
    store, row, lease, baseline, software = setup_worker
    result = worker_v3.process(store, row, lease, baseline, software)
    assert lease.events[-1][0] == "ESCALATED"
    assert len(result["diagnoses"]) == 1


@pytest.mark.parametrize("repeat", [False, True])
def test_reinvestigation_new_action_waits_for_fresh_approval(
    setup_worker, monkeypatch, tmp_path, repeat
):
    monkeypatch.setenv("CLOUDSRE_H2B_ENABLED", "1")
    monkeypatch.setenv("CLOUDSRE_DIAG_URL", "http://diagnostic")
    monkeypatch.setenv("CLOUDSRE_DIAG_TOKEN", "test-only")
    store, row, lease, baseline, software = setup_worker
    journal = worker_v3.process(store, row, lease, baseline, software)
    new_id = journal["diagnoses"][-1]["id"]
    new_folder = tmp_path / new_id
    (new_folder / "evidence").mkdir(parents=True)
    (new_folder / "holmes-result.json").write_text(json.dumps({"result": "proposal"}))
    store.runs = lambda ident: [
        {"payload": journal, "phase": "INVESTIGATING", "id": "run"}
    ]
    proposal = SimpleNamespace(
        action="restore_replicas",
        resource="sre-demo/Deployment/inventory",
        value=1,
        model_dump=lambda: {
            "action": "restore_replicas",
            "resource": "sre-demo/Deployment/inventory",
            "value": 1,
        },
    )
    if repeat:
        proposal.action = "restore_env"
        proposal.resource = "sre-demo/Deployment/order"
        proposal.value = "old"
    new_intent = {"id": "new-action", "resource": proposal.resource, "value": 1}
    monkeypatch.setattr(worker_v3, "parse", lambda raw: proposal)
    monkeypatch.setattr(worker_v3, "authorize", lambda *args: new_intent)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"state": "complete"}

    monkeypatch.setattr(worker_v3.httpx, "post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(worker_v3.httpx, "get", lambda *args, **kwargs: Response())
    result = worker_v3.process(store, row, lease, baseline, software)
    if repeat:
        assert lease.events[-1][0] == "ESCALATED"
        assert "Repeated unsuccessful action" in result["error"]
        return
    assert lease.events[-1][0] == "WAITING_APPROVAL"
    assert result["action_intent"]["id"] == "new-action"
    assert result["h2b_history"][0]["action_intent"]["id"] == "old-action"
    assert result["outcome"] == "awaiting_approval"


def test_warmup_timeout_can_reinvestigate_repeated_business_failure(
    setup_worker, monkeypatch
):
    monkeypatch.setenv("CLOUDSRE_H2B_ENABLED", "1")
    monkeypatch.setattr(
        worker_v3,
        "request",
        lambda path: {
            "passed": False,
            "checks": {},
            "check": "create_order",
            "http_status": 503,
            "cleanup": {"cleaned": True},
        },
    )
    monkeypatch.setattr(
        worker_v3.quality_module,
        "RecoveryReadiness",
        lambda: SimpleNamespace(observe=lambda *args: False),
    )
    tick = iter(range(10000))
    monkeypatch.setattr(worker_v3.time, "monotonic", lambda: next(tick))
    monkeypatch.setattr(worker_v3.time, "sleep", lambda seconds: None)
    store, row, lease, baseline, software = setup_worker
    result = worker_v3.process(store, row, lease, baseline, software)
    assert lease.events[-1][0] == "INVESTIGATING"
    assert result["h2b_feedback"]["failed_checks"] == ["create_order_http_5xx"]
    assert result["h2b_history"][0]["verification"]["kind"] == "warmup_timeout"
