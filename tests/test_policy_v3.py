import copy

import pytest
from cloudsre.actions import snapshot
from cloudsre.policy_v3 import Proposal, authorize
from pydantic import ValidationError
from test_actions_v3 import baseline, resource


def case():
    raw = resource(name="inventory")
    trusted = baseline(raw)
    raw["spec"]["replicas"] = 0
    identity = snapshot(raw)
    proposal = Proposal(
        action="restore_replicas",
        resource=identity["resource"],
        expected_spec_hash=identity["spec_hash"],
        value=1,
        reason="Observed zero replicas and a verified healthy baseline",
        evidence_ids=["current", "healthy", "runtime"],
    )
    evidence = [
        {
            "id": "current",
            "kind": "resources",
            "data": {
                "resources": [
                    {"identity": identity, "spec": copy.deepcopy(raw["spec"])}
                ]
            },
        },
        {
            "id": "healthy",
            "kind": "baseline",
            "data": {"verification_sha256": trusted["verification_sha256"]},
        },
        {
            "id": "runtime",
            "kind": "observations",
            "data": {"pods": [], "endpoints": []},
        },
    ]
    return proposal, evidence, raw, trusted


def test_evidence_bound_replica_repair_compiles():
    proposal, evidence, raw, trusted = case()
    assert (
        authorize(proposal, evidence, raw, trusted, "sre-demo")["action"]
        == "restore_replicas"
    )


@pytest.mark.parametrize("fault", ["citation", "baseline", "identity", "namespace"])
def test_unbound_evidence_cannot_authorize_a_write(fault):
    proposal, evidence, raw, trusted = case()
    if fault == "citation":
        proposal.evidence_ids.append("fabricated")
    elif fault == "baseline":
        evidence[1]["data"]["verification_sha256"] = "not-the-trusted-baseline"
    elif fault == "identity":
        evidence[0]["data"]["resources"][0]["identity"]["uid"] = "different-resource"
    else:
        proposal.resource = "sre-demo-b/Deployment/inventory"
    with pytest.raises(ValueError):
        authorize(proposal, evidence, raw, trusted, "sre-demo")


@pytest.mark.parametrize(
    ("action", "value"),
    [
        ("restore_env", {"name": "INVENTORY_URL", "value": "http://inventory:8000"}),
        ("restore_replicas", True),
        ("restore_service_port", None),
        ("restore_resources", "192Mi"),
    ],
)
def test_wrong_parameter_shape_is_rejected_before_execution(action, value):
    with pytest.raises(ValidationError):
        Proposal(
            action=action,
            resource="sre-demo/Deployment/order",
            expected_spec_hash="observed",
            value=value,
            reason="format test",
            evidence_ids=["observed"],
        )
