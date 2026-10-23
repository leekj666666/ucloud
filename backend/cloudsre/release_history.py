"""Sanitized immutable release snapshots and hash-chained evidence."""

import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

PUBLIC_ENV = {"SERVICE_ROLE", "RELEASE_ID", "INVENTORY_URL", "ORDER_URL"}


def digest(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot(raw):
    meta = raw["metadata"]
    result = {
        "name": meta["name"],
        "namespace": meta.get("namespace", "sre-demo"),
        "uid": meta["uid"],
        "generation": meta.get("generation"),
        "resource_version": meta["resourceVersion"],
        "created_at": meta["creationTimestamp"],
        "revision": meta.get("annotations", {}).get(
            "deployment.kubernetes.io/revision"
        ),
        "containers": [
            {
                "name": c["name"],
                "image": c["image"],
                "env": {
                    e["name"]: e.get("value")
                    for e in c.get("env", [])
                    if e["name"] in PUBLIC_ENV
                },
            }
            for c in raw["spec"]["template"]["spec"]["containers"]
        ],
    }

    result["image_content_id"] = (
        raw["spec"]["template"]
        .get("metadata", {})
        .get("annotations", {})
        .get("cloudsre/image-id")
    )
    result["public_config_hash"] = digest(result["containers"])
    return result


def changes(before, after):
    result = {}

    def flatten(item):
        return {
            c["name"] + "." + key: val
            for c in item.get("containers", [])
            for key, val in {"image": c["image"], **c["env"]}.items()
        }

    old, new = flatten(before), flatten(after)
    for key in old.keys() | new.keys():
        if old.get(key) != new.get(key):
            result[key] = {"before": old.get(key), "after": new.get(key)}
    if before.get("image_content_id") != after.get("image_content_id"):
        result["image_content_id"] = {
            "before": before.get("image_content_id"),
            "after": after.get("image_content_id"),
        }
    return result


def record(raw, folder, *, healthy=False, verification_ref=None, replicas=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    current = snapshot(raw)
    earlier = []
    for path in sorted(folder.glob("*.json")):
        previous = json.loads(path.read_text())
        if previous["snapshot"]["name"] == current["name"]:
            earlier.append(previous)
    prior = earlier[-1] if earlier else None
    payload = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "source": "kubernetes-api",
        "snapshot": current,
        "diff": changes(prior["snapshot"], current) if prior else {},
        "previous_hash": prior["content_hash"] if prior else None,
        "healthy_verified": healthy,
        "verification_ref": verification_ref,
        "revisions": [
            snapshot(r)
            for r in (replicas or [])
            if any(
                o.get("uid") == current["uid"]
                for o in r["metadata"].get("ownerReferences", [])
            )
        ],
    }
    if healthy and not verification_ref:
        raise ValueError("Healthy release requires independent verification reference")
    payload["content_hash"] = digest(payload)
    path = folder / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        + "-"
        + uuid.uuid4().hex[:8]
        + ".json"
    )
    path.write_text(json.dumps(payload, indent=2))
    return payload


def collect(root, *, healthy=False, verification_ref=None):
    kubectl = str(Path.home() / "sre-tools/bin/kubectl")
    prefix = [kubectl, "--context", "kind-sre-dev", "-n", "sre-demo", "get"]

    def read(kind):
        return json.loads(subprocess.check_output(prefix + [kind, "-o", "json"]))[
            "items"
        ]

    replicas = read("replicaset")
    records = [
        record(
            r,
            Path(root) / "evaluation/releases",
            healthy=healthy,
            verification_ref=verification_ref,
            replicas=replicas,
        )
        for r in read("deployment")
        if r["metadata"]["name"] in {"gateway", "order", "inventory"}
    ]
    return records
