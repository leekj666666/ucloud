import copy
import json
import subprocess

import pytest
from cloudsre import kube


def deployment():
    return {
        "metadata": {"uid": "target", "generation": 2, "resourceVersion": "10"},
        "spec": {
            "template": {
                "metadata": {"annotations": {"cloudsre/image-id": "sha256:a"}},
                "spec": {
                    "containers": [
                        {
                            "name": "order",
                            "image": "app:v1",
                            "env": [
                                {"name": "INVENTORY_URL", "value": "http://old:8000"},
                                {"name": "RELEASE_ID", "value": "r1"},
                            ],
                        }
                    ]
                },
            }
        },
    }


@pytest.mark.parametrize(
    "change",
    ["uid", "generation", "resourceVersion", "image", "image_content_id", "spec"],
)
def test_stale_policy_binding_never_writes(monkeypatch, change):
    raw = deployment()
    approved = kube.deployment_snapshot(raw)
    if change in {"uid", "resourceVersion"}:
        raw["metadata"][change] = "changed"
    elif change == "generation":
        raw["metadata"][change] += 1
    elif change == "image":
        raw["spec"]["template"]["spec"]["containers"][0]["image"] = "app:v2"
    elif change == "image_content_id":
        raw["spec"]["template"]["metadata"]["annotations"]["cloudsre/image-id"] = (
            "sha256:b"
        )
    else:
        raw["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "limits": {"memory": "128Mi"}
        }
    monkeypatch.setattr(kube, "read", lambda *a: raw)
    writes = []
    monkeypatch.setattr(kube.subprocess, "run", lambda *a, **k: writes.append(a))
    with pytest.raises(ValueError, match="changed before execution"):
        kube.guarded_patch(approved, "http://old:8000", "http://inventory:8000", "id")
    assert writes == []


@pytest.mark.parametrize("applied", [True, False])
def test_unknown_outcome_reconciles_without_resending(monkeypatch, applied):
    raw = deployment()
    approved = kube.deployment_snapshot(raw)
    writes = []
    monkeypatch.setattr(kube, "read", lambda *a: copy.deepcopy(raw))

    def run(args, **kwargs):
        if "patch" in args:
            writes.append(args)
            patch = json.loads(args[-1])
            assert {p["path"] for p in patch if p["op"] == "test"} >= {
                "/metadata/uid",
                "/metadata/generation",
                "/metadata/resourceVersion",
            }
            if applied:
                raw["metadata"].update(generation=3, resourceVersion="11")
                env = raw["spec"]["template"]["spec"]["containers"][0]["env"]
                env[0]["value"], env[1]["value"] = "http://inventory:8000", "r-id"
            raise subprocess.TimeoutExpired(args, 20)

    monkeypatch.setattr(kube.subprocess, "run", run)
    if applied:
        assert kube.guarded_patch(
            approved, "http://old:8000", "http://inventory:8000", "id"
        )["ambiguous_write_reconciled"]
    else:
        with pytest.raises(RuntimeError, match="no automatic retry"):
            kube.guarded_patch(
                approved, "http://old:8000", "http://inventory:8000", "id"
            )
    assert len(writes) == 1


def test_atomic_api_rejection_cannot_be_called_success(monkeypatch):
    raw = deployment()
    approved = kube.deployment_snapshot(raw)
    monkeypatch.setattr(kube, "read", lambda *a: copy.deepcopy(raw))
    writes = []

    def conflict(args, **kwargs):
        if "patch" in args:
            writes.append(args)
            raw["metadata"]["resourceVersion"] = "12"
            raw["spec"]["template"]["spec"]["containers"][0]["image"] = "app:concurrent"
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(kube.subprocess, "run", conflict)
    with pytest.raises(RuntimeError, match="no automatic retry"):
        kube.guarded_patch(approved, "http://old:8000", "http://inventory:8000", "id")
    assert len(writes) == 1
    assert (
        raw["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"]
        == "http://old:8000"
    )


@pytest.mark.parametrize("field,value", [("namespace", "other"), ("name", "inventory")])
def test_wrong_target_never_writes(monkeypatch, field, value):
    snapshot = kube.deployment_snapshot(deployment())
    snapshot[field] = value
    writes = []
    monkeypatch.setattr(kube.subprocess, "run", lambda *a, **k: writes.append(a))
    with pytest.raises(ValueError, match="Unsupported"):
        kube.guarded_patch(snapshot, "http://old:8000", "http://inventory:8000", "id")
    assert writes == []
