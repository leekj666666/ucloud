import copy

import pytest
from cloudsre.actions import (
    OP_KEY,
    make_intent,
    prepare_bounded_scale,
    prepare_compensation,
)
from test_actions_v3 import baseline, resource


def scaled():
    raw = resource()
    prior = {"passed": True, "tenant": "independent-verification"}
    intent = prepare_bounded_scale(raw, 2, baseline(raw), prior)
    current = copy.deepcopy(raw)
    current["spec"]["replicas"] = 2
    current["metadata"]["resourceVersion"] = "13"
    current["metadata"]["annotations"][OP_KEY] = intent["id"]
    return intent, current, prior


def test_compensation_binds_own_action_and_verified_prior():
    intent, current, prior = scaled()
    compensation = prepare_compensation(intent, current, prior)
    assert compensation["compensates"] == intent["id"]
    assert compensation["after_spec_hash"] == intent["before"]["spec_hash"]
    assert {
        "op": "test",
        "path": "/metadata/annotations/cloudsre~1operation-id",
        "value": intent["id"],
    } in compensation["patch"]


@pytest.mark.parametrize("change", ["spec", "operation", "verification", "prior"])
def test_compensation_refuses_concurrent_change_or_unbound_evidence(change):
    intent, current, prior = scaled()
    if change == "spec":
        current["spec"]["replicas"] = 3
    elif change == "operation":
        current["metadata"]["annotations"][OP_KEY] = "another-action"
    elif change == "verification":
        prior["tenant"] = "different-verification"
    else:
        intent["before_spec"]["replicas"] = 99
    with pytest.raises(ValueError):
        prepare_compensation(intent, current, prior)


def test_repair_cannot_compensate_back_to_known_bad_state():
    raw = resource()
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    intent = make_intent(raw, "restore_replicas", 1, trusted)
    with pytest.raises(ValueError, match="unhealthy"):
        prepare_compensation(intent, raw, {"passed": True})
