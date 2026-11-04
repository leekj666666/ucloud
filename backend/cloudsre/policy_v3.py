"""Evidence-bound D3 proposals; model reasoning never grants mutation authority."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .actions import make_intent, snapshot
from .dependency_symptoms import matches_dependency_failure


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "restore_env",
        "restore_service_port",
        "restore_replicas",
        "restore_resources",
        "escalate",
    ]
    resource: str
    expected_spec_hash: str
    value: str | int | dict | None
    reason: str = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def action_value_type(self):
        allowed = {
            "restore_env": (str,),
            "restore_replicas": (int,),
            "restore_service_port": (int, str),
            "restore_resources": (dict,),
            "escalate": (type(None),),
        }
        if type(self.value) not in allowed[self.action]:
            raise ValueError("Action value has an invalid type")
        return self


def parse(answer):
    answer = answer.strip()
    if answer.startswith("```"):
        answer = answer.split("\n", 1)[1].rsplit("```", 1)[0]
    return Proposal.model_validate(json.loads(answer))


def authorize(plan, evidence, current, baseline, namespace):
    if plan.action == "escalate":
        raise ValueError("Investigator requested human review")
    if (
        not plan.resource.startswith(namespace + "/")
        or plan.resource != snapshot(current)["resource"]
    ):
        raise ValueError("Proposal targets a different incident namespace/resource")
    if plan.expected_spec_hash != snapshot(current)["spec_hash"]:
        raise ValueError("Proposed resource version is stale")
    selected = {e["id"]: e for e in evidence if e["id"] in plan.evidence_ids}
    if set(selected) != set(plan.evidence_ids):
        raise ValueError("Unknown evidence citation")
    resources = [
        r
        for e in selected.values()
        if e["kind"] == "resources"
        for r in e["data"]["resources"]
    ]
    identity = snapshot(current)
    if not any(
        all(
            r["identity"][k] == identity[k]
            for k in ("resource", "uid", "generation", "spec_hash", "operation_id")
        )
        for r in resources
    ):
        raise ValueError("Current resource identity/version evidence missing")
    if not any(
        e["kind"] == "baseline"
        and e["data"].get("verification_sha256") == baseline["verification_sha256"]
        for e in selected.values()
    ):
        raise ValueError("Verified baseline must be cited")
    runtime = [e["data"] for e in selected.values() if e["kind"] == "observations"]
    if not runtime:
        raise ValueError("Runtime counter-evidence required")
    pods = [p for data in runtime for p in data["pods"]]
    name = current["metadata"]["name"]
    relevant = [
        p
        for p in pods
        if p.get("labels", {}).get("app") == name or p["name"].startswith(name + "-")
    ]
    if plan.action == "restore_resources":
        if not any(
            c.get("lastState", {}).get("terminated", {}).get("reason") == "OOMKilled"
            or c.get("state", {}).get("terminated", {}).get("reason") == "OOMKilled"
            for p in relevant
            for c in p["status"].get("containerStatuses", [])
        ):
            raise ValueError("No target OOM evidence; blind resource change refused")
    elif plan.action == "restore_replicas":
        if current["spec"]["replicas"] != 0:
            raise ValueError(
                "Replica repair requires observed dependency scaled to zero"
            )
    elif plan.action == "restore_service_port":
        container_ports = [
            port["containerPort"]
            for p in relevant
            for c in p.get("containers", [])
            for port in c.get("ports", [])
        ]
        # The verified Deployment is independently read evidence, not an injected answer.
        container_ports += [
            port["containerPort"]
            for r in resources
            if r["identity"]["resource"] == namespace + "/Deployment/" + name
            for c in r["spec"]["template"]["spec"]["containers"]
            for port in c.get("ports", [])
        ]
        if plan.value not in container_ports:
            raise ValueError("Target port not supported by live workload evidence")
    elif plan.action == "restore_env" and name == "order":
        field = next(
            e["value"]
            for c in current["spec"]["template"]["spec"]["containers"]
            for e in c.get("env", [])
            if e["name"] == "INVENTORY_URL"
        )
        logs = [
            line["line"]
            for e in selected.values()
            if e["kind"] == "logs"
            for line in e["data"].get("lines", [])
        ]
        if not matches_dependency_failure(logs, field, plan.value):
            raise ValueError("Matching dependency failure logs required")
        endpoints = [
            s
            for data in runtime
            for s in data["endpoints"]
            if s["metadata"].get("labels", {}).get("kubernetes.io/service-name")
            == "inventory"
        ]
        if not any(
            e.get("conditions", {}).get("ready")
            for s in endpoints
            for e in s.get("endpoints", [])
        ):
            raise ValueError(
                "Inventory itself unhealthy; do not mask dependency outage"
            )
    return make_intent(current, plan.action, plan.value, baseline)
