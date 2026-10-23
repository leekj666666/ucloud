import copy
import json
from types import SimpleNamespace

import pytest
from cloudsre import worker
from cloudsre.domain import IncidentInput
from cloudsre.quality import evaluate
from cloudsre.store import Store


def records():
    return (
        [
            {
                "kind": "load",
                "scheduling_delay_seconds": 0,
                "result": {"passed": True, "http_status": 200, "latency_seconds": 0.1},
            }
            for _ in range(120)
        ]
        + [
            {
                "kind": "business",
                "result": {"passed": True, "cleanup": {"cleaned": True}},
            }
            for _ in range(12)
        ]
        + [{"kind": "monitor", "result": {"alerts": []}} for _ in range(24)]
        + [
            {
                "kind": "resources",
                "result": {"containers": [{"MemUsage": "1MiB / 100MiB"}]},
            }
        ]
    )


def test_full_window_and_latency_threshold():
    assert evaluate(records(), 120, 0.1)["passed"]
    assert not evaluate(records(), 30, 0.1)["passed"]
    data = records()
    for row in data[:120]:
        row["result"]["latency_seconds"] = 1.1
    assert not evaluate(data, 120, 0.1)["passed"]


@pytest.mark.parametrize(
    "problem",
    [
        "load_error",
        "transport",
        "business",
        "monitor_missing",
        "new_alert",
        "schedule",
        "too_few",
    ],
)
def test_false_recovery_is_rejected(problem):
    data = copy.deepcopy(records())
    if problem == "load_error":
        data[80]["result"].update(passed=False, http_status=503)
    elif problem == "transport":
        data[80].pop("result")
        data[80]["error_type"] = "ReadTimeout"
    elif problem == "business":
        data[125]["result"]["passed"] = False
    elif problem == "monitor_missing":
        data[140].pop("result")
        data[140]["error_type"] = "ReadTimeout"
    elif problem == "new_alert":
        data[140]["result"]["alerts"] = [
            {"metric": {"severity": "critical", "service": "order"}}
        ]
    elif problem == "schedule":
        data[80]["scheduling_delay_seconds"] = 2
    else:
        data = data[1:]
    assert not evaluate(data, 120, 0.1)["passed"]


def test_healthy_endpoint_but_failed_window_never_resolves(monkeypatch, tmp_path):
    root = tmp_path
    store = Store("sqlite:///" + str(root / "incidents.db"))
    store.initialize()
    row, _ = store.create(
        IncidentInput(dedup_key="state-negative", title="OrderErrors", service="order")
    )
    baseline = root / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "passed": True,
                "duration_seconds": 120,
                "p95_seconds": 0.1,
                "samples_sha256": "test",
            }
        )
    )
    monkeypatch.setenv("CLOUDSRE_HEALTHY_BASELINE", str(baseline))
    monkeypatch.setattr(worker, "ROOT", root)
    monkeypatch.setattr(worker, "versions", lambda p: {"test_fixture": True})
    monkeypatch.setattr(worker, "dotenv_values", lambda p: {})
    monkeypatch.setattr(worker, "summarize", lambda p: {"http_attempts": 1})
    monkeypatch.setattr(worker, "read", lambda *a: {})
    snapshot = {"uid": "x", "generation": 1, "spec_hash": "test", "env": {}}
    monkeypatch.setattr(worker, "deployment_snapshot", lambda r: snapshot)
    monkeypatch.setattr(worker, "inventory_snapshot", dict)
    monkeypatch.setattr(worker, "desired_url", lambda: "http://inventory:8000")
    monkeypatch.setattr(
        worker,
        "validate_plan",
        lambda *a: {"old": "http://old:8000", "new": "http://inventory:8000"},
    )
    monkeypatch.setattr(worker, "guarded_patch", lambda *a: snapshot)
    monkeypatch.setattr(worker, "request", lambda *a: {"passed": True})
    monkeypatch.setattr(
        worker,
        "run_window",
        lambda *a: {"passed": False, "checks": {"all_invariants_succeeded": False}},
    )

    def diagnose(args, **kwargs):
        folder = root / "evaluation/runs"
        run = next(folder.iterdir())
        proposal = {
            "action": "restore_env",
            "namespace": "sre-demo",
            "deployment": "order",
            "field": "INVENTORY_URL",
            "expected_current": "http://old:8000",
            "proposed_value": "http://inventory:8000",
            "reason": "test",
            "evidence_ids": ["e"],
        }
        (run / "holmes-result.json").write_text(
            json.dumps({"result": json.dumps(proposal), "num_llm_calls": 1})
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", diagnose)
    result = worker.run_incident(store, row)
    assert store.get(row["id"])["status"] == "ESCALATED"
    assert result["outcome"] == "needs_human"
    assert "RESOLVED" not in [e["phase"] for e in result["events"]]
