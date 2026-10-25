"""An evaluator may retry resource-version races but must not overwrite drift."""

import copy
import json
import subprocess
import threading

import pytest

from benchmarks.d5 import live
from benchmarks.d5.catalog import DEV


def setup_fixture(tmp_path):
    fixture = live.Fixture(DEV[0], tmp_path)
    fixture.stop = threading.Event()
    fixture.record.update(
        active=True,
        changes=[
            {
                "kind": "service",
                "name": "inventory",
                "uid": "original-uid",
                "path": "/spec/value",
                "original": 8000,
                "injected": 8011,
            }
        ],
    )
    return fixture


def test_cleanup_retries_status_version_race_with_fresh_identity_checks(
    tmp_path, monkeypatch
):
    fixture = setup_fixture(tmp_path)
    current = {
        "metadata": {"uid": "original-uid", "resourceVersion": "1"},
        "spec": {"value": 8011},
    }
    writes = []
    monkeypatch.setattr(live, "read", lambda *_: copy.deepcopy(current))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    def kube(*args):
        patch = json.loads(args[-1])
        writes.append(patch)
        if len(writes) == 1:
            current["metadata"]["resourceVersion"] = "2"
            raise subprocess.CalledProcessError(1, ["kubectl"])
        assert patch[1]["value"] == "2"
        assert patch[2]["value"] == 8011
        current["spec"]["value"] = patch[-1]["value"]

    monkeypatch.setattr(live, "kube", kube)
    assert fixture.cleanup() == ["inventory/spec/value"]
    assert current["spec"]["value"] == 8000 and len(writes) == 2
    assert fixture.record["active"] is False


@pytest.mark.parametrize("drift", ["uid", "field"])
def test_cleanup_never_overwrites_a_foreign_value_after_conflict(
    tmp_path, monkeypatch, drift
):
    fixture = setup_fixture(tmp_path)
    current = {
        "metadata": {"uid": "original-uid", "resourceVersion": "1"},
        "spec": {"value": 8011},
    }
    writes = []
    monkeypatch.setattr(live, "read", lambda *_: copy.deepcopy(current))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    def kube(*args):
        writes.append(args)
        if drift == "uid":
            current["metadata"]["uid"] = "replacement-uid"
        else:
            current["spec"]["value"] = 9999
        raise subprocess.CalledProcessError(1, ["kubectl"])

    monkeypatch.setattr(live, "kube", kube)
    with pytest.raises(ValueError, match="Recreated|Foreign"):
        fixture.cleanup()
    assert len(writes) == 1 and fixture.record["active"] is True
