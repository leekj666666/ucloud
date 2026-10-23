"""Narrow Kubernetes operations for this local demo. No model shell commands."""

import json
import os
import subprocess
from pathlib import Path

KUBECTL = str(Path.home() / "sre-tools/bin/kubectl")
PREFIX = [KUBECTL, "--context", "kind-sre-dev", "-n", "sre-demo"]


def read(resource, name=None):
    if resource not in {"deployment", "replicaset", "service", "endpointslice"}:
        raise ValueError("Unsupported read resource")
    args = PREFIX + ["get", resource]
    if name:
        if name not in {"order", "inventory"}:
            raise ValueError("Unsupported resource name")
        args.append(name)
    result = subprocess.run(
        args + ["-o", "json"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, "GOMAXPROCS": "2", "GOMEMLIMIT": "128MiB"},
    )
    if result.returncode:
        raise RuntimeError("Kubernetes read failed: " + result.stderr[:1800])
    return json.loads(result.stdout)


def deployment_snapshot(raw):
    container = next(
        c for c in raw["spec"]["template"]["spec"]["containers"] if c["name"] == "order"
    )
    env = {
        e["name"]: e.get("value")
        for e in container.get("env", [])
        if e["name"] in {"INVENTORY_URL", "RELEASE_ID"}
    }
    return {
        "uid": raw["metadata"]["uid"],
        "resource_version": raw["metadata"]["resourceVersion"],
        "generation": raw["metadata"]["generation"],
        "name": "order",
        "namespace": "sre-demo",
        "image": container["image"],
        "image_content_id": raw["spec"]["template"]
        .get("metadata", {})
        .get("annotations", {})
        .get("cloudsre/image-id"),
        "env": env,
        "ready_replicas": raw.get("status", {}).get("readyReplicas", 0),
    }


def inventory_snapshot():
    service = read("service", "inventory")
    slices = read("endpointslice")["items"]
    ready = sum(
        1
        for s in slices
        if s["metadata"].get("labels", {}).get("kubernetes.io/service-name")
        == "inventory"
        for e in s.get("endpoints", [])
        if e.get("conditions", {}).get("ready")
    )
    return {
        "service": "inventory",
        "namespace": "sre-demo",
        "ports": [p["port"] for p in service["spec"]["ports"]],
        "ready_endpoints": ready,
    }


def guarded_patch(snapshot, old, new, incident_id):
    # JSON Patch test is an atomic concurrency precondition on the API server.
    raw = read("deployment", "order")
    current = deployment_snapshot(raw)
    if current["uid"] != snapshot["uid"] or current["env"].get("INVENTORY_URL") != old:
        raise ValueError("Deployment changed before execution")
    containers = raw["spec"]["template"]["spec"]["containers"]
    ci = next(i for i, c in enumerate(containers) if c["name"] == "order")
    env = containers[ci].get("env", [])
    ei = next(i for i, e in enumerate(env) if e["name"] == "INVENTORY_URL")
    ri = next(i for i, e in enumerate(env) if e["name"] == "RELEASE_ID")
    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": current["resource_version"],
        },
        {
            "op": "test",
            "path": f"/spec/template/spec/containers/{ci}/env/{ei}/value",
            "value": old,
        },
        {
            "op": "replace",
            "path": f"/spec/template/spec/containers/{ci}/env/{ei}/value",
            "value": new,
        },
        {
            "op": "replace",
            "path": f"/spec/template/spec/containers/{ci}/env/{ri}/value",
            "value": "d2-repair-" + incident_id[:8],
        },
    ]
    subprocess.run(
        PREFIX
        + ["patch", "deployment", "order", "--type=json", "-p", json.dumps(patch)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    subprocess.run(
        PREFIX + ["rollout", "status", "deployment/order", "--timeout=120s"],
        check=True,
        capture_output=True,
        text=True,
        timeout=130,
    )
    after = deployment_snapshot(read("deployment", "order"))
    if after["env"].get("INVENTORY_URL") != new:
        raise RuntimeError("Patch readback failed")
    return after
