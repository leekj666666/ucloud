"""Immutable, version-bound D3 mutation intents and read-before-retry reconciliation."""

import copy
import uuid

from .durable import digest

NAMESPACES = {"sre-demo", "sre-demo-b"}
TARGETS = {
    "Deployment": {"order", "inventory", "gateway"},
    "Service": {"order", "inventory", "gateway"},
}
OP_KEY = "cloudsre/operation-id"
OP_PATH = "/metadata/annotations/cloudsre~1operation-id"


def resource_key(raw):
    kind, meta = raw["kind"], raw["metadata"]
    if (
        kind not in TARGETS
        or meta.get("namespace") not in NAMESPACES
        or meta["name"] not in TARGETS[kind]
    ):
        raise ValueError("Target outside action allowlist")
    return "/".join((meta["namespace"], kind, meta["name"]))


def snapshot(raw):
    key = resource_key(raw)
    return {
        "resource": key,
        "uid": raw["metadata"]["uid"],
        "resource_version": raw["metadata"]["resourceVersion"],
        "generation": raw["metadata"].get("generation"),
        "spec_hash": digest(raw["spec"]),
        "operation_id": raw["metadata"].get("annotations", {}).get(OP_KEY),
    }


def make_intent(raw, action, value, baseline, *, action_id=None):
    key = resource_key(raw)
    trusted = baseline["resources"].get(key)
    if (
        not baseline.get("healthy_verified")
        or not baseline.get("verification_sha256")
        or not trusted
    ):
        raise ValueError("Verified resource baseline required")
    if trusted["metadata"]["uid"] != raw["metadata"]["uid"]:
        raise ValueError("Trusted baseline resource recreated")
    expected = copy.deepcopy(raw["spec"])
    edits = []
    if action == "restore_service_port":
        if raw["kind"] != "Service" or raw["metadata"]["name"] not in {
            "inventory",
            "order",
            "gateway",
        }:
            raise ValueError("Invalid service target")
        before_ports, healthy_ports = expected["ports"], trusted["spec"]["ports"]
        if (
            len(before_ports) != 1
            or len(healthy_ports) != 1
            or value != healthy_ports[0]["targetPort"]
        ):
            raise ValueError("Port differs from verified baseline")
        path = "/spec/ports/0/targetPort"
        edits = [
            {"op": "test", "path": path, "value": before_ports[0]["targetPort"]},
            {"op": "replace", "path": path, "value": value},
        ]
        before_ports[0]["targetPort"] = value
    elif action in {"restore_env", "restore_resources", "restore_replicas"}:
        if raw["kind"] != "Deployment":
            raise ValueError("Deployment action requires Deployment")
        if action == "restore_replicas":
            if (
                value != trusted["spec"]["replicas"]
                or type(value) is not int
                or not 1 <= value <= 2
            ):
                raise ValueError("Replica count outside verified bounds")
            edits = [
                {"op": "test", "path": "/spec/replicas", "value": expected["replicas"]},
                {"op": "replace", "path": "/spec/replicas", "value": value},
            ]
            expected["replicas"] = value
        else:
            name = raw["metadata"]["name"]
            containers = expected["template"]["spec"]["containers"]
            ci = next(i for i, c in enumerate(containers) if c["name"] == name)
            healthy = next(
                c
                for c in trusted["spec"]["template"]["spec"]["containers"]
                if c["name"] == name
            )
            container = containers[ci]
            if container["image"] != healthy["image"]:
                raise ValueError("Image changed; baseline compatibility unproven")
            if action == "restore_env":
                field = {"order": "INVENTORY_URL", "gateway": "ORDER_URL"}.get(name)
                if field is None:
                    raise ValueError("No permitted environment restore")
                ei = next(
                    i for i, e in enumerate(container["env"]) if e["name"] == field
                )
                desired = next(e["value"] for e in healthy["env"] if e["name"] == field)
                if value != desired:
                    raise ValueError("Environment differs from verified baseline")
                path = f"/spec/template/spec/containers/{ci}/env/{ei}/value"
                old = container["env"][ei]["value"]
                container["env"][ei]["value"] = value
            else:
                if value != healthy.get("resources") or not isinstance(value, dict):
                    raise ValueError("Resource limits differ from verified baseline")
                path = f"/spec/template/spec/containers/{ci}/resources"
                old = container.get("resources", {})
                container["resources"] = copy.deepcopy(value)
            edits = [
                {"op": "test", "path": path, "value": old},
                {"op": "replace", "path": path, "value": value},
            ]
    else:
        raise ValueError("Unknown action")
    if expected == raw["spec"]:
        raise ValueError("No-op mutation refused")
    ident = action_id or uuid.uuid4().hex
    meta = raw["metadata"]
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": meta["resourceVersion"],
        },
        {"op": "test", "path": "/spec", "value": raw["spec"]},
    ]
    if "annotations" not in meta:
        patch.append({"op": "add", "path": "/metadata/annotations", "value": {}})
    patch.extend(edits)
    patch.append({"op": "add", "path": OP_PATH, "value": ident})
    return {
        "id": ident,
        "resource": key,
        "action": action,
        "value": value,
        "before": snapshot(raw),
        "after_spec_hash": digest(expected),
        "patch": patch,
        "healthy_baseline_hash": digest(baseline),
        "prior_spec_known_unhealthy": True,
    }


def classify(intent, current):
    actual = snapshot(current)
    before = intent["before"]
    if actual["uid"] != before["uid"] or actual["resource"] != intent["resource"]:
        return "CONFLICT"
    if (
        actual["operation_id"] == intent["id"]
        and actual["spec_hash"] == intent["after_spec_hash"]
    ):
        return "APPLIED"
    if all(
        actual[k] == before[k]
        for k in ("uid", "resource_version", "spec_hash", "operation_id")
    ):
        return "NOT_APPLIED"
    return "CONFLICT"


def reconcile_and_apply(intent, read, patch, lease_check, record_dispatch):
    """Delayed/repeated identical requests can only have one Kubernetes effect."""
    lease_check()
    current = read(intent["resource"])
    state = classify(intent, current)
    if state == "APPLIED":
        return {
            "id": intent["id"],
            "state": "APPLIED",
            "after": snapshot(current),
            "reconciled": True,
            "http_attempted": False,
        }
    if state == "CONFLICT":
        return {
            "id": intent["id"],
            "state": "CONFLICT",
            "after": snapshot(current),
            "http_attempted": False,
        }
    record_dispatch(intent)
    lease_check()
    error = None
    try:
        patch(intent["resource"], intent["patch"])
    except (TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
        error = type(exc).__name__
    # Read failure propagates: persistent DISPATCHED intent survives for takeover.
    current = read(intent["resource"])
    state = classify(intent, current)
    if state == "NOT_APPLIED":
        state = "REJECTED" if error else "CONFLICT"
    return {
        "id": intent["id"],
        "state": state,
        "after": snapshot(current),
        "reconciled": error is not None,
        "write_error_type": error,
        "http_attempted": True,
    }


def compensation_allowed(intent, independently_verified_prior):
    # Restoring the broken pre-repair state is never a valid compensation.
    return (
        not intent.get("prior_spec_known_unhealthy", True)
        and independently_verified_prior is True
    )


def prepare_bounded_scale(raw, replicas, baseline, prior_business):
    """A bounded scaling action with an independently observed compensation baseline."""
    key = resource_key(raw)
    trusted = baseline["resources"].get(key)
    if (
        raw["kind"] != "Deployment"
        or not baseline.get("healthy_verified")
        or not baseline.get("verification_sha256")
        or not trusted
        or trusted["metadata"]["uid"] != raw["metadata"]["uid"]
    ):
        raise ValueError("Scaling target lacks trusted baseline")
    if (
        type(replicas) is not int
        or not 1 <= replicas <= 2
        or raw["spec"]["replicas"] == replicas
    ):
        raise ValueError("Scaling outside bounded range or no-op")
    if not prior_business.get("passed") or digest(trusted["spec"]) != digest(
        raw["spec"]
    ):
        raise ValueError(
            "Compensatable scaling requires current independently healthy baseline"
        )
    expected = copy.deepcopy(raw["spec"])
    expected["replicas"] = replicas
    ident = uuid.uuid4().hex
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": raw["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": raw["metadata"]["resourceVersion"],
        },
        {"op": "test", "path": "/spec", "value": raw["spec"]},
    ]
    if "annotations" not in raw["metadata"]:
        patch.append({"op": "add", "path": "/metadata/annotations", "value": {}})
    patch += [
        {"op": "replace", "path": "/spec/replicas", "value": replicas},
        {"op": "add", "path": OP_PATH, "value": ident},
    ]
    return {
        "id": ident,
        "resource": key,
        "action": "scale_within_limit",
        "value": replicas,
        "before": snapshot(raw),
        "before_spec": copy.deepcopy(raw["spec"]),
        "after_spec_hash": digest(expected),
        "patch": patch,
        "healthy_baseline_hash": digest(baseline),
        "prior_spec_known_unhealthy": False,
        "prior_business_sha256": digest(prior_business),
    }


def prepare_compensation(intent, current, prior_business):
    if not compensation_allowed(intent, prior_business.get("passed")):
        raise ValueError("Compensation would restore an unhealthy or unverified state")
    if digest(prior_business) != intent.get("prior_business_sha256"):
        raise ValueError("Prior business verification binding differs")
    if classify(intent, current) != "APPLIED":
        raise ValueError("Concurrent change prevents compensation")
    if digest(intent["before_spec"]) != intent["before"]["spec_hash"]:
        raise ValueError("Compensation prior state changed")
    ident = uuid.uuid4().hex
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": current["metadata"]["resourceVersion"],
        },
        {"op": "test", "path": "/spec", "value": current["spec"]},
        {"op": "test", "path": OP_PATH, "value": intent["id"]},
        {"op": "replace", "path": "/spec", "value": intent["before_spec"]},
        {"op": "add", "path": OP_PATH, "value": ident},
    ]
    return {
        "id": ident,
        "resource": intent["resource"],
        "action": "compensate",
        "compensates": intent["id"],
        "before": snapshot(current),
        "after_spec_hash": intent["before"]["spec_hash"],
        "patch": patch,
        "prior_spec_known_unhealthy": False,
    }
