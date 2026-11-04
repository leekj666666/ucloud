import json

import httpx
import pytest
from cloudsre import product_diagnosis as product
from cloudsre.experiment_views import load_records


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(product.time, "sleep", lambda seconds: None)
    (tmp_path / "incident.json").write_text(
        json.dumps({"namespace": "sre-demo", "evidence_since": "2026-11-05T00:00:00Z"})
    )
    identity = {
        "resource": "sre-demo/Deployment/order",
        "spec_hash": "current",
        "uid": "same",
    }
    data = {
        "resources": {"resources": [{"identity": identity, "spec": {"replicas": 0}}]},
        "baseline": {
            "resources": [
                {
                    "identity": identity,
                    "spec": {"replicas": 1, "template": {"spec": {"containers": []}}},
                }
            ]
        },
        "observations": {"pods": [], "endpoints": []},
        "logs": {"lines": []},
        "releases": {},
        "metrics": {},
    }
    monkeypatch.setattr(product, "collect", lambda kind, folder: data[kind])
    return tmp_path, data


def test_rule_path_has_actual_snapshot_citations_and_no_authority(scene):
    folder, _ = scene
    receipt = product.prepare(folder, "h5")
    assert receipt["route"] == "rule"
    proposal = json.loads((folder / "proposal.json").read_text())
    records = load_records(folder / "snapshot")
    assert proposal["action"] == "restore_replicas"
    assert set(proposal["evidence_ids"]) == {r["id"] for r in records.values()}
    assert receipt["execution_authorized"] is False
    assert receipt["approval_required"] is True
    assert len(list((folder / "evidence").glob("*.json"))) == 6


def test_switch_bypasses_rule_and_preserves_model_route(scene):
    folder, _ = scene
    assert product.prepare(folder, "b3")["route"] == "model"
    assert not (folder / "proposal.json").exists()


def test_unsupported_scene_falls_back_without_repair(scene):
    folder, data = scene
    data["resources"]["resources"][0]["spec"] = {
        "replicas": 1,
        "template": {"spec": {"containers": []}},
    }
    assert product.prepare(folder, "h5")["route"] == "model"
    assert not (folder / "proposal.json").exists()


@pytest.mark.parametrize("kind", product.KINDS)
def test_collection_error_prevents_rule_shortcut(scene, monkeypatch, kind):
    folder, data = scene

    def collect(name, folder):
        if name == kind:
            raise httpx.ConnectError("unavailable")
        return data[name]

    monkeypatch.setattr(product, "collect", collect)
    result = product.prepare(folder, "h5")
    assert result["route"] == "model" and result["collection_errors"] == [kind]
    assert not (folder / "proposal.json").exists()


def test_existing_snapshot_cannot_be_overwritten(scene):
    folder, _ = scene
    product.prepare(folder, "h5")
    with pytest.raises(FileExistsError):
        product.prepare(folder, "h5")


def test_delayed_logs_are_read_again_and_original_zero_is_retained(scene, monkeypatch):
    folder, data = scene
    attempts = []

    def collect(kind, folder):
        if kind == "logs":
            attempts.append(kind)
            return {
                "lines": []
                if len(attempts) < 3
                else [{"line": "actual newly shipped log"}]
            }
        return data[kind]

    monkeypatch.setattr(product, "collect", collect)
    product.prepare(folder, "h5")
    assert len(attempts) == 3
    saved = sorted((folder / "log-collection-attempts").glob("*.json"))
    assert len(saved) == 3
    assert json.loads(saved[0].read_text())["data"]["lines"] == []
    assert (
        load_records(folder / "snapshot")["logs"]["data"]["lines"][0]["line"]
        == "actual newly shipped log"
    )


def test_permanently_empty_logs_have_finite_read_budget(scene, monkeypatch):
    folder, data = scene
    attempts = []

    def collect(kind, folder):
        if kind == "logs":
            attempts.append(kind)
        return data[kind]

    monkeypatch.setattr(product, "collect", collect)
    product.prepare(folder, "h5")
    assert len(attempts) <= 7
