import copy

import pytest
from cloudsre.actions import classify, make_intent, reconcile_and_apply, snapshot
from cloudsre.durable import digest


def resource(kind="Deployment", name="order"):
    spec = (
        {"ports": [{"port": 8000, "targetPort": 8000}]}
        if kind == "Service"
        else {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": "demo:verified",
                            "env": [
                                {
                                    "name": "INVENTORY_URL",
                                    "value": "http://inventory:8000",
                                }
                            ],
                            "resources": {"limits": {"memory": "128Mi", "cpu": "200m"}},
                        }
                    ]
                }
            },
        }
    )
    return {
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "sre-demo",
            "uid": "u-" + name,
            "resourceVersion": "12",
            "generation": 1,
            "annotations": {},
        },
        "spec": spec,
    }


def baseline(raw):
    return {
        "healthy_verified": True,
        "verification_sha256": "verified-reference",
        "resources": {snapshot(raw)["resource"]: copy.deepcopy(raw)},
    }


@pytest.mark.parametrize(
    "action",
    ["restore_env", "restore_service_port", "restore_replicas", "restore_resources"],
)
def test_four_actions_bind_full_old_spec_uid_version_and_baseline(action):
    raw = resource("Service" if action == "restore_service_port" else "Deployment")
    trusted = baseline(raw)
    if action == "restore_env":
        value = "http://inventory:8000"
        raw["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = (
            "http://unreachable:8000"
        )
    elif action == "restore_service_port":
        value = 8000
        raw["spec"]["ports"][0]["targetPort"] = 9000
    elif action == "restore_replicas":
        value = 1
        raw["spec"]["replicas"] = 0
    else:
        value = {"limits": {"memory": "128Mi", "cpu": "200m"}}
        raw["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
            "memory"
        ] = "8Mi"
    intent = make_intent(raw, action, value, trusted)
    assert intent["patch"][:3] == [
        {"op": "test", "path": "/metadata/uid", "value": "u-order"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "12"},
        {"op": "test", "path": "/spec", "value": raw["spec"]},
    ]
    assert intent["after_spec_hash"] == digest(
        trusted["resources"][intent["resource"]]["spec"]
    )
    assert intent["prior_spec_known_unhealthy"] is True


def test_unknown_response_after_commit_reconciles_without_second_effect():
    raw = resource()
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    intent = make_intent(raw, "restore_replicas", 1, trusted)
    current = copy.deepcopy(raw)
    writes = []

    def patch(key, ops):
        writes.append(ops)
        current["spec"]["replicas"] = 1
        current["metadata"].update(resourceVersion="13", generation=2)
        current["metadata"]["annotations"]["cloudsre/operation-id"] = intent["id"]
        raise TimeoutError("response lost after commit")

    result = reconcile_and_apply(
        intent, lambda _: current, patch, lambda: None, lambda _: None
    )
    assert result["state"] == "APPLIED" and result["reconciled"]
    replay = reconcile_and_apply(
        intent, lambda _: current, patch, lambda: None, lambda _: None
    )
    assert replay["state"] == "APPLIED" and not replay["http_attempted"]
    assert len(writes) == 1


def test_matching_desired_state_without_our_operation_id_is_not_our_success():
    raw = resource()
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    intent = make_intent(raw, "restore_replicas", 1, trusted)
    foreign = copy.deepcopy(raw)
    foreign["spec"]["replicas"] = 1
    foreign["metadata"]["resourceVersion"] = "13"
    assert classify(intent, foreign) == "CONFLICT"


def test_write_timeout_without_observed_effect_does_not_retry_blindly():
    raw = resource()
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    intent = make_intent(raw, "restore_replicas", 1, trusted)
    writes = []

    def patch(key, ops):
        writes.append(ops)
        raise TimeoutError()

    result = reconcile_and_apply(
        intent, lambda _: raw, patch, lambda: None, lambda _: None
    )
    assert result["state"] == "REJECTED" and len(writes) == 1


@pytest.mark.parametrize("bad", ["target", "uid", "baseline", "image", "replica_limit"])
def test_invalid_mutations_rejected(bad):
    raw = resource()
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    if bad == "target":
        raw["metadata"]["name"] = "probe"
    if bad == "uid":
        raw["metadata"]["uid"] = "recreated"
    if bad == "baseline":
        trusted["healthy_verified"] = False
    if bad == "image":
        raw["spec"]["template"]["spec"]["containers"][0]["image"] = "unverified"
        raw["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "bad"
        action, value = "restore_env", "http://inventory:8000"
    else:
        action, value = "restore_replicas", 100 if bad == "replica_limit" else 1
    with pytest.raises(ValueError):
        make_intent(raw, action, value, trusted)


def test_foreign_spec_change_prevents_patch():
    raw = resource()
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    intent = make_intent(raw, "restore_replicas", 1, trusted)
    current = copy.deepcopy(raw)
    current["metadata"]["resourceVersion"] = "13"

    def forbidden(*args):
        raise AssertionError("Unexpected write")

    assert (
        reconcile_and_apply(
            intent, lambda _: current, forbidden, lambda: None, forbidden
        )["state"]
        == "CONFLICT"
    )


def test_readback_outage_retains_intent_and_takeover_does_not_repeat_write():
    raw = resource()
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    intent = make_intent(raw, "restore_replicas", 1, trusted)
    current = copy.deepcopy(raw)
    calls = []

    def patch(key, ops):
        calls.append(ops)
        current["spec"]["replicas"] = 1
        current["metadata"]["resourceVersion"] = "13"
        current["metadata"]["annotations"]["cloudsre/operation-id"] = intent["id"]

    def read(key):
        if calls:
            raise TimeoutError("Readback endpoint unavailable")
        return current

    with pytest.raises(TimeoutError):
        reconcile_and_apply(intent, read, patch, lambda: None, lambda _: None)
    resumed = reconcile_and_apply(
        intent, lambda _: current, patch, lambda: None, lambda _: None
    )
    assert resumed["state"] == "APPLIED" and not resumed["http_attempted"]
    assert len(calls) == 1
