"""Narrow Kubernetes operations for this local demo. No model shell commands."""

import copy
import hashlib
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


def spec_digest(spec):
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
        "spec_hash": spec_digest(raw["spec"]),
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
    if snapshot.get("name") != "order" or snapshot.get("namespace") != "sre-demo":
        raise ValueError("Unsupported execution target")
    raw = read("deployment", "order")
    current = deployment_snapshot(raw)
    binding = (
        "uid",
        "generation",
        "resource_version",
        "image",
        "image_content_id",
        "spec_hash",
    )
    if (
        any(current.get(k) != snapshot.get(k) for k in binding)
        or current["env"].get("INVENTORY_URL") != old
    ):
        raise ValueError(
            "Deployment changed before execution; reinvestigation required"
        )
    ci = next(
        i
        for i, c in enumerate(raw["spec"]["template"]["spec"]["containers"])
        if c["name"] == "order"
    )
    env = raw["spec"]["template"]["spec"]["containers"][ci]["env"]
    ei = next(i for i, e in enumerate(env) if e["name"] == "INVENTORY_URL")
    ri = next(i for i, e in enumerate(env) if e["name"] == "RELEASE_ID")
    release = "r-" + incident_id.replace("-", "")[:16]
    expected = copy.deepcopy(raw["spec"])
    expected_env = expected["template"]["spec"]["containers"][ci]["env"]
    expected_env[ei]["value"] = new
    expected_env[ri]["value"] = release
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": current["uid"]},
        {"op": "test", "path": "/metadata/generation", "value": current["generation"]},
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
            "value": release,
        },
    ]
    ambiguous = False
    try:
        subprocess.run(
            PREFIX
            + ["patch", "deployment", "order", "--type=json", "-p", json.dumps(patch)],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        ambiguous = True
    # Every write outcome is read back. Never resend a possibly applied mutation.
    after = deployment_snapshot(read("deployment", "order"))
    if (
        after["uid"] != current["uid"]
        or after["generation"] != current["generation"] + 1
        or after["spec_hash"] != spec_digest(expected)
    ):
        raise RuntimeError(
            "Action outcome unknown, rejected, or changed; no automatic retry"
        )
    subprocess.run(
        PREFIX + ["rollout", "status", "deployment/order", "--timeout=120s"],
        check=True,
        capture_output=True,
        text=True,
        timeout=130,
    )
    final = deployment_snapshot(read("deployment", "order"))
    if any(final[k] != after[k] for k in ("uid", "generation", "spec_hash")):
        raise RuntimeError("Deployment changed during rollout")
    final["ambiguous_write_reconciled"] = ambiguous
    return final
