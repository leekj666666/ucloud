import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["restore_env", "escalate"]
    namespace: Literal["sre-demo"]
    deployment: Literal["order"]
    field: Literal["INVENTORY_URL"]
    expected_current: str
    proposed_value: str
    reason: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


def parse_proposal(answer):
    answer = answer.strip()
    if answer.startswith("```"):
        answer = answer.split("\n", 1)[1].rsplit("```", 1)[0]
    return Proposal.model_validate(json.loads(answer))


def validate_plan(plan, evidence, current, desired, inventory):
    if plan.action != "restore_env":
        raise ValueError("Model requested escalation")
    selected = {e["id"]: e for e in evidence if e["id"] in plan.evidence_ids}
    if set(selected) != set(plan.evidence_ids):
        raise ValueError("Unrecognized evidence ID")
    if plan.proposed_value != desired:
        raise ValueError("Proposed value differs from trusted baseline")
    if plan.expected_current == desired:
        raise ValueError("Configuration is already healthy; refusing blind repair")
    if current["env"].get("INVENTORY_URL") != plan.expected_current:
        raise ValueError("Stale expected configuration")
    deployment = [
        e["data"]["current"] for e in selected.values() if e["kind"] == "deployment"
    ]
    if not any(
        d["uid"] == current["uid"]
        and d["generation"] == current["generation"]
        and d.get("spec_hash") == current.get("spec_hash")
        and d.get("image") == current.get("image")
        and d["env"].get("INVENTORY_URL") == plan.expected_current
        for d in deployment
    ):
        raise ValueError("Missing current deployment evidence")
    if inventory["ready_endpoints"] < 1 or 8000 not in inventory["ports"]:
        raise ValueError("Healthy inventory service not established")
    if not any(
        e["kind"] == "inventory" and e["data"]["ready_endpoints"] > 0
        for e in selected.values()
    ):
        raise ValueError("Inventory evidence was not cited")
    matching_error = False
    for e in selected.values():
        if e["kind"] != "logs":
            continue
        for stream in e["data"]["response"]["data"]["result"]:
            for _, line in stream.get("values", []):
                try:
                    log = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    log.get("event") == "inventory_unavailable"
                    and log.get("upstream") == plan.expected_current
                ):
                    matching_error = True
    if not matching_error:
        raise ValueError("Missing matching upstream failure log")
    return {
        "permitted": True,
        "action": "restore_env",
        "field": plan.field,
        "old": plan.expected_current,
        "new": desired,
    }
