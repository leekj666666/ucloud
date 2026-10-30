import json
from collections import Counter

import pytest

from benchmarks.d5 import live
from benchmarks.d6 import protocol
from benchmarks.d6.fixture import namespace


def test_formal_schedule_preserves_60_trials_per_system_and_three_repeats():
    tasks = protocol.create_schedule(20261031)
    assert len(tasks) == 180 and len({t["id"] for t in tasks}) == 180
    assert Counter(t["variant"] for t in tasks) == {"B0": 60, "B2": 60, "B3": 60}
    assert all(
        n == 3 for n in Counter((t["case_id"], t["variant"]) for t in tasks).values()
    )
    assert tasks == protocol.create_schedule(20261031)


def test_holdout_cannot_be_read_without_explicit_open(monkeypatch, tmp_path):
    monkeypatch.setattr(protocol, "PRIVATE", tmp_path)
    monkeypatch.setattr(protocol, "SEALED", tmp_path / "must-not-read.json")
    monkeypatch.setattr(protocol, "verify_protocol", dict)
    with pytest.raises(ValueError, match="explicitly opened"):
        protocol.case_by_id("holdout_01")


def test_freeze_hashes_seal_without_parsing_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(protocol, "PRIVATE", tmp_path / "private")
    sealed = tmp_path / "sealed"
    sealed.write_bytes(b"not JSON; must not parse during freeze")
    monkeypatch.setattr(protocol, "SEALED", sealed)
    monkeypatch.setattr(protocol, "source_hashes", lambda: {"app": "sha"})
    monkeypatch.setattr(protocol.subprocess, "check_output", lambda *a, **k: "commit\n")
    plan = protocol.freeze("sha256:image", "test-model")
    assert plan["default_H2b"] is False and plan["default_H5"] is False
    assert not (protocol.PRIVATE / "opened.json").exists()
    with pytest.raises(FileExistsError):
        protocol.freeze("sha256:image", "test-model")


def test_changed_seal_prevents_opening(monkeypatch, tmp_path):
    monkeypatch.setattr(protocol, "PRIVATE", tmp_path)
    sealed = tmp_path / "sealed"
    sealed.write_text("{}")
    monkeypatch.setattr(protocol, "SEALED", sealed)
    monkeypatch.setattr(protocol, "source_hashes", lambda: {"app": "same"})
    (tmp_path / "protocol.json").write_text(
        json.dumps({"source_hashes": {"app": "same"}, "sealed_sha256": "different"})
    )
    with pytest.raises(ValueError, match="changed"):
        protocol.open_holdout()
    assert not (tmp_path / "opened.json").exists()


def test_namespace_restored_after_cross_namespace_failure():
    original = live.KUBE
    with pytest.raises(RuntimeError), namespace("sre-demo-b"):
        assert live.KUBE[-1] == "sre-demo-b"
        raise RuntimeError("simulated API failure")
    assert live.KUBE is original


def test_baseline_and_model_drift_prevent_opening(monkeypatch, tmp_path):
    import dotenv

    monkeypatch.setattr(protocol, "ROOT", tmp_path)
    monkeypatch.setattr(protocol, "PRIVATE", tmp_path)
    sealed = tmp_path / "sealed"
    sealed.write_text("{}")
    monkeypatch.setattr(protocol, "SEALED", sealed)
    baseline = tmp_path / "infra/.generated/d4/baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("{}")
    monkeypatch.setattr(protocol, "source_hashes", lambda: {"app": "same"})
    plan = {
        "source_hashes": {"app": "same"},
        "sealed_sha256": protocol.checksum(sealed),
        "baseline_sha256": "changed",
        "model": "frozen-model",
    }
    (tmp_path / "protocol.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="baseline changed"):
        protocol.open_holdout()
    plan["baseline_sha256"] = protocol.checksum(baseline)
    (tmp_path / "protocol.json").write_text(json.dumps(plan))
    monkeypatch.setattr(
        dotenv, "dotenv_values", lambda *args: {"MODEL_NAME": "different-model"}
    )
    with pytest.raises(ValueError, match="model name changed"):
        protocol.open_holdout()
    assert not (tmp_path / "opened.json").exists()
