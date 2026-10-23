import json

import pytest
from cloudsre.domain import IncidentInput
from cloudsre.policy import Proposal, parse_proposal, validate_plan
from cloudsre.store import Store
from cloudsre.worker import eligible
from pydantic import ValidationError


def case():
    current = {
        "uid": "live-uid",
        "generation": 4,
        "env": {"INVENTORY_URL": "http://wrong:8000"},
    }
    evidence = [
        {"id": "config", "kind": "deployment", "data": {"current": current}},
        {"id": "svc", "kind": "inventory", "data": {"ready_endpoints": 1}},
        {
            "id": "logs",
            "kind": "logs",
            "data": {
                "response": {
                    "data": {
                        "result": [
                            {
                                "values": [
                                    [
                                        "now",
                                        json.dumps(
                                            {
                                                "event": "inventory_unavailable",
                                                "upstream": "http://wrong:8000",
                                            }
                                        ),
                                    ]
                                ]
                            }
                        ]
                    }
                }
            },
        },
    ]
    plan = Proposal(
        action="restore_env",
        namespace="sre-demo",
        deployment="order",
        field="INVENTORY_URL",
        expected_current="http://wrong:8000",
        proposed_value="http://inventory:8000",
        reason="Bad upstream configuration corroborated by request errors",
        evidence_ids=["config", "svc", "logs"],
    )
    return plan, evidence, current, {"ready_endpoints": 1, "ports": [8000]}


def test_validated_plan_and_fake_evidence_rejected():
    p, e, c, i = case()
    assert validate_plan(p, e, c, "http://inventory:8000", i)["permitted"]
    with pytest.raises(ValueError, match="evidence ID"):
        validate_plan(
            p.model_copy(update={"evidence_ids": ["invented"]}),
            e,
            c,
            "http://inventory:8000",
            i,
        )


def test_changed_configuration_and_wrong_destination_rejected():
    p, e, c, i = case()
    with pytest.raises(ValueError, match="Stale"):
        validate_plan(
            p,
            e,
            {**c, "env": {"INVENTORY_URL": "http://someone-else:8000"}},
            "http://inventory:8000",
            i,
        )
    with pytest.raises(ValueError, match="baseline"):
        validate_plan(
            p.model_copy(update={"proposed_value": "http://external:8000"}),
            e,
            c,
            "http://inventory:8000",
            i,
        )
    with pytest.raises(ValueError, match="Healthy inventory"):
        validate_plan(
            p, e, c, "http://inventory:8000", {"ready_endpoints": 0, "ports": [8000]}
        )


def test_unrelated_logs_and_out_of_scope_plan_rejected():
    p, e, c, i = case()
    with pytest.raises(ValueError, match="failure log"):
        validate_plan(
            p.model_copy(update={"evidence_ids": ["config", "svc"]}),
            e[:2],
            c,
            "http://inventory:8000",
            i,
        )
    with pytest.raises(ValidationError):
        parse_proposal(json.dumps({**p.model_dump(), "namespace": "kube-system"}))
    with pytest.raises(ValidationError):
        parse_proposal(
            json.dumps({**p.model_dump(), "shell_command": "arbitrary command"})
        )


def test_resolved_alert_never_triggers_worker(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "alerts.db"))
    s.initialize()
    row, _ = s.create(
        IncidentInput(dedup_key="one", title="OrderErrors", service="order")
    )
    assert not eligible(s, row)
    s.record_alert(row["id"], "one", "firing", {})
    assert eligible(s, row)
    s.record_alert(row["id"], "one", "resolved", {})
    assert not eligible(s, row)
