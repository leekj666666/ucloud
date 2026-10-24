"""Read-only rollout health metrics; no administrator or evaluator access."""

from .kube_gateway import KubeGateway


def oom_services(pods):
    result = {name: 0 for name in ("gateway", "order", "inventory")}
    for pod in pods:
        name = pod.get("metadata", {}).get("labels", {}).get("app")
        if name not in result or pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        for container in pod.get("status", {}).get("containerStatuses", []):
            reasons = [
                container.get(key, {}).get("terminated", {}).get("reason")
                for key in ("state", "lastState")
            ]
            if not container.get("ready") and "OOMKilled" in reasons:
                result[name] = 1
    return result


def render(namespaces):
    lines = ["# TYPE cloudsre_active_oom gauge"]
    gateway = KubeGateway("read")
    try:
        for namespace in namespaces:
            if namespace not in {"sre-demo", "sre-demo-b"}:
                raise ValueError("Metrics namespace outside allowlist")
            pods = gateway.observations(namespace)["pods"]
            for service, value in oom_services(pods).items():
                lines.append(
                    f'cloudsre_active_oom{{namespace="{namespace}",service="{service}"}} {value}'
                )
    finally:
        gateway.close()
    return "\n".join(lines) + "\n"
