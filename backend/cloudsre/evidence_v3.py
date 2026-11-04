"""Fixed read-only evidence collection; evaluation inputs are never mounted here."""

import copy
import json
import os
import uuid
from pathlib import Path

import httpx

from .actions import NAMESPACES, snapshot
from .durable import digest, now
from .kube_gateway import KubeGateway
from .release_history import snapshot as release_snapshot

PUBLIC_ENV = {"INVENTORY_URL", "ORDER_URL", "RELEASE_ID", "SERVICE_ROLE"}


def public_resource(raw):
    result = {
        "identity": snapshot(raw),
        "created_at": raw["metadata"]["creationTimestamp"],
        "annotations": {
            k: v
            for k, v in raw["metadata"].get("annotations", {}).items()
            if k in {"deployment.kubernetes.io/revision", "cloudsre/operation-id"}
        },
        "status": raw.get("status", {}),
        "spec": copy.deepcopy(raw["spec"]),
    }
    for c in result["spec"].get("template", {}).get("spec", {}).get("containers", []):
        c["env"] = [
            e for e in c.get("env", []) if e["name"] in PUBLIC_ENV and "value" in e
        ]
        c.pop("envFrom", None)
    return result


def release_revisions(replicasets):
    """ReplicaSet creation is rollout evidence, not a claimed CI publish timestamp."""
    records = []
    for raw in replicasets:
        owners = [
            {"name": o["name"], "uid": o["uid"]}
            for o in raw["metadata"].get("ownerReferences", [])
            if o.get("kind") == "Deployment"
            and o.get("controller") is True
            and o.get("name") in {"gateway", "order", "inventory"}
        ]
        if not owners:
            continue
        records.append(
            {
                **release_snapshot(raw),
                "owners": owners,
                "timestamp_semantics": "ReplicaSet creation time; not CI publication time",
            }
        )
    return sorted(records, key=lambda item: (item["created_at"], item["uid"]))


def collect(kind, folder):
    incident = json.loads((folder / "incident.json").read_text())
    namespace = incident["namespace"]
    if namespace not in NAMESPACES:
        raise ValueError("Evidence namespace outside allowlist")
    since = incident["evidence_since"]
    if kind in {"resources", "observations", "baseline", "releases"}:
        gateway = KubeGateway("read")
        try:
            if kind == "resources":
                return {
                    "resources": [
                        public_resource(gateway.read(namespace + "/" + k + "/" + n))
                        for k in ("Deployment", "Service")
                        for n in ("gateway", "order", "inventory")
                    ]
                }
            if kind == "releases":
                return {
                    "release_revisions": release_revisions(gateway.releases(namespace))
                }
            if kind == "observations":
                data = gateway.observations(namespace)
                data["pods"] = [
                    {
                        "name": p["metadata"]["name"],
                        "labels": p["metadata"].get("labels", {}),
                        "owners": p["metadata"].get("ownerReferences", []),
                        "status": p.get("status", {}),
                        "containers": [
                            {
                                "name": c["name"],
                                "image": c["image"],
                                "resources": c.get("resources", {}),
                            }
                            for c in p["spec"]["containers"]
                        ],
                    }
                    for p in data["pods"]
                ]
                return data
            baseline = json.loads(
                Path(os.environ["CLOUDSRE_TRUSTED_BASELINE"]).read_text()
            )
            return {
                "healthy_verified": baseline["healthy_verified"],
                "verification_sha256": baseline["verification_sha256"],
                "recorded_at": baseline["recorded_at"],
                "resources": [
                    public_resource(v)
                    for k, v in baseline["resources"].items()
                    if k.startswith(namespace + "/")
                ],
            }
        finally:
            gateway.close()
    if kind == "logs":
        query = '{namespace="' + namespace + '"} | json | event=~"inventory_unavailable|inventory_rejected"'
        r = httpx.get(
            os.environ["CLOUDSRE_LOKI_URL"] + "/loki/api/v1/query_range",
            params={"query": query, "start": since, "limit": 24},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        return {
            "query": query,
            "query_start": since,
            "raw": body,
            "lines": [
                {"timestamp": ts, "line": line}
                for stream in body["data"]["result"]
                for ts, line in stream["values"]
            ],
        }
    if kind == "metrics":
        # The empty namespace alternative supports the original single-graph scrape labels.
        namespace_match = namespace + ("|^$" if namespace == "sre-demo" else "")
        query = (
            'sum by(service,status)(rate(sre_http_requests_total{route="/orders",namespace=~"'
            + namespace_match
            + '"}[1m]))'
        )
        r = httpx.get(
            os.environ["CLOUDSRE_PROM_URL"] + "/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        r.raise_for_status()
        return {"query": query, "response": r.json()}
    raise ValueError("Unsupported evidence kind")


def compact_release_timeline(revisions):
    """Keep every fetched revision; intern repeated configurations and owner IDs."""
    groups = {}
    for item in revisions:
        for owner in item["owners"]:
            group = groups.setdefault(
                owner["uid"],
                {
                    "deployment": owner["name"],
                    "deployment_uid": owner["uid"],
                    "configurations": [],
                    "revisions": [],
                },
            )
            config = {
                "containers": item["containers"],
                "image_content_id": item.get("image_content_id"),
            }
            if config not in group["configurations"]:
                group["configurations"].append(config)
            group["revisions"].append(
                [
                    item["uid"],
                    item["created_at"],
                    item.get("revision"),
                    group["configurations"].index(config),
                ]
            )
    return {
        "timestamp_semantics": "ReplicaSet creation time; not CI publication time",
        "columns": ["replicaset_uid", "created_at", "revision", "configuration_index"],
        "revision_count": len(revisions),
        "groups": list(groups.values()),
    }


def model_view(record):
    view = copy.deepcopy(record)
    data = view["data"]
    data.pop("raw", None)
    if record["kind"] in {"resources", "baseline"}:
        for item in data["resources"]:
            spec = item["spec"]
            if "template" in spec:
                item["spec"] = {
                    "replicas": spec["replicas"],
                    "containers": [
                        {
                            k: c[k]
                            for k in ("name", "image", "env", "ports", "resources")
                            if k in c
                        }
                        for c in spec["template"]["spec"]["containers"]
                    ],
                }
            else:
                item["spec"] = {
                    k: spec[k] for k in ("ports", "selector", "type") if k in spec
                }
            item.pop("status", None)
    if "release_revisions" in data:
        data["release_timeline"] = compact_release_timeline(
            data.pop("release_revisions")
        )
    if record["kind"] == "observations":
        pods = data["pods"]
        data["pods"] = [
            {
                "name": p["name"],
                "phase": p["status"].get("phase"),
                "conditions": [
                    {
                        "type": c["type"],
                        "status": c["status"],
                        "reason": c.get("reason"),
                    }
                    for c in p["status"].get("conditions", [])
                ],
                "containers": p["containers"],
                "container_states": [
                    {
                        k: c[k]
                        for k in ("name", "ready", "restartCount", "state", "lastState")
                        if k in c
                    }
                    for c in p["status"].get("containerStatuses", [])
                ],
            }
            for p in pods
            if any(
                p["name"].startswith(n + "-") for n in ("order", "inventory", "gateway")
            )
        ]
        events = sorted(
            data["events"],
            key=lambda e: e.get("lastTimestamp") or e["metadata"]["creationTimestamp"],
        )
        data["event_count_fetched"] = len(events)
        data["event_view_limit"] = 12
        data["events"] = [
            {
                "resource": e.get("involvedObject", {}).get("name"),
                "reason": e.get("reason"),
                "type": e.get("type"),
                "message": e.get("message"),
                "last_at": e.get("lastTimestamp"),
                "count": e.get("count"),
            }
            for e in events[-12:]
        ]
        data["endpoints"] = [
            {
                "service": e["metadata"]
                .get("labels", {})
                .get("kubernetes.io/service-name"),
                "ports": e.get("ports"),
                "endpoints": e.get("endpoints"),
            }
            for e in data["endpoints"]
        ]
    view["raw_evidence_retained"] = True
    return view


def main():
    import sys

    kind = sys.argv[1]
    folder = Path(os.environ["CLOUDSRE_RUN_DIR"])
    data = collect(kind, folder)
    record = {
        "id": "e-" + uuid.uuid4().hex,
        "kind": kind,
        "collected_at": now(),
        "source": "live-read-only-tool",
        "data": data,
        "content_hash": digest(data),
    }
    (folder / "evidence").mkdir(exist_ok=True)
    (folder / "evidence" / (record["id"] + ".json")).write_text(
        json.dumps(record, ensure_ascii=False)
    )
    view = model_view(record)
    print(json.dumps(view, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
