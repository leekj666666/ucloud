"""Conservative B0/H5 rules over the same observations available to all models."""

from .experiment_views import KINDS


def propose(records, namespace):
    live = {
        item["identity"]["resource"]: item
        for item in records["resources"]["data"]["resources"]
    }
    healthy = {
        item["identity"]["resource"]: item
        for item in records["baseline"]["data"]["resources"]
    }
    runtime = records["observations"]["data"]
    candidates = []

    def candidate(item, action, value, reason):
        candidates.append(
            {
                "action": action,
                "resource": item["identity"]["resource"],
                "expected_spec_hash": item["identity"]["spec_hash"],
                "value": value,
                "reason": reason,
                "evidence_ids": [records[kind]["id"] for kind in KINDS],
            }
        )

    def ready(name):
        return any(
            endpoint.get("conditions", {}).get("ready")
            for group in runtime.get("endpoints", [])
            if group["metadata"].get("labels", {}).get("kubernetes.io/service-name")
            == name
            for endpoint in group.get("endpoints", [])
        )

    for key, item in live.items():
        baseline = healthy.get(key)
        if not baseline:
            continue
        kind, name = key.split("/")[1:]
        if kind == "Service":
            old = item["spec"]["ports"]
            expected = baseline["spec"]["ports"]
            deployment = live.get(namespace + "/Deployment/" + name)
            ports = (
                [
                    p["containerPort"]
                    for c in deployment["spec"]["template"]["spec"]["containers"]
                    for p in c.get("ports", [])
                ]
                if deployment
                else []
            )
            if (
                item["spec"].get("selector") == baseline["spec"].get("selector")
                and len(old) == len(expected) == 1
                and old[0]["targetPort"] != expected[0]["targetPort"]
                and expected[0]["targetPort"] in ports
            ):
                candidate(
                    item,
                    "restore_service_port",
                    expected[0]["targetPort"],
                    "The Service targetPort differs from both its verified baseline and the observed workload port; selector identity agrees.",
                )
            continue
        if item["spec"]["replicas"] == 0 and 1 <= baseline["spec"]["replicas"] <= 2:
            candidate(
                item,
                "restore_replicas",
                baseline["spec"]["replicas"],
                "Observed desired replica count is zero; the verified same-UID workload baseline has a bounded nonzero count.",
            )
            continue
        containers = item["spec"]["template"]["spec"]["containers"]
        before = baseline["spec"]["template"]["spec"]["containers"]
        pod_states = [
            state
            for pod in runtime.get("pods", [])
            if pod.get("labels", {}).get("app") == name
            for state in pod["status"].get("containerStatuses", [])
        ]
        oom = any(
            state.get(key, {}).get("terminated", {}).get("reason") == "OOMKilled"
            for state in pod_states
            for key in ("state", "lastState")
        )
        for container in containers:
            prior = next((c for c in before if c["name"] == container["name"]), None)
            if not prior:
                continue
            if oom and container.get("resources") != prior.get("resources"):
                candidate(
                    item,
                    "restore_resources",
                    prior["resources"],
                    "Current target containers have observed OOM termination and resource settings differ from their independently verified same-UID baseline.",
                )
            field = {"order": "INVENTORY_URL", "gateway": "ORDER_URL"}.get(name)
            if not field:
                continue
            current = next(
                (
                    e.get("value")
                    for e in container.get("env", [])
                    if e["name"] == field
                ),
                None,
            )
            expected = next(
                (e.get("value") for e in prior.get("env", []) if e["name"] == field),
                None,
            )
            upstream = {"order": "inventory", "gateway": "order"}[name]
            logs = records["logs"]["data"].get("lines", [])
            log_match = any(
                current
                and current in row["line"]
                and "inventory_unavailable" in row["line"]
                for row in logs
            )
            if (
                current
                and expected
                and current != expected
                and ready(upstream)
                and (name == "gateway" or log_match)
            ):
                candidate(
                    item,
                    "restore_env",
                    expected,
                    "A public dependency URL differs from the verified baseline while the intended upstream has ready endpoints; matching failure symptoms support restoring this scalar setting.",
                )
    if len(candidates) == 1:
        return candidates[0]
    default = live[namespace + "/Deployment/order"]
    return {
        "action": "escalate",
        "resource": default["identity"]["resource"],
        "expected_spec_hash": default["identity"]["spec_hash"],
        "value": None,
        "reason": "The fixed rule coverage does not establish exactly one supported causal action; request model investigation or human review.",
        "evidence_ids": [records[kind]["id"] for kind in KINDS],
    }
