"""Restore restart-dependent private addresses; no Agent recovery credit."""

import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path("/home/lee/sre-project")
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
import httpx
from dotenv import dotenv_values

from benchmarks.d5.live import WORK, exclusive


def run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw)


def address(name):
    info = json.loads(run(["docker", "inspect", name]).stdout)[0]
    return info["NetworkSettings"]["Networks"]["sre-infra_default"]["IPAddress"]


k = ["/home/lee/sre-tools/bin/kubectl", "--context", "kind-sre-dev"]
with exclusive():
    password = dotenv_values(ROOT / "infra/.env.demo")["DEMO_PASSWORD"]
    uri = (
        "postgresql+psycopg://sre_demo:"
        + quote(password, safe="")
        + "@"
        + address("sre-infra-postgres-1")
        + ":5432/sre_demo"
    )
    changed = []
    for namespace in ("sre-demo", "sre-demo-b"):
        old = json.loads(
            run(k + ["-n", namespace, "get", "secret", "demo-db", "-o", "json"]).stdout
        )
        replacement = base64.b64encode(uri.encode()).decode()
        if old["data"]["DATABASE_URL"] != replacement:
            patch = {"data": {"DATABASE_URL": replacement}}
            run(
                k
                + [
                    "-n",
                    namespace,
                    "patch",
                    "secret",
                    "demo-db",
                    "--type=merge",
                    "--patch-file=/dev/stdin",
                ],
                input=json.dumps(patch),
            )
            run(
                k
                + [
                    "-n",
                    namespace,
                    "delete",
                    "pods",
                    "-l",
                    "app in (gateway,order,inventory,probe)",
                    "--wait=false",
                ]
            )
            changed.append(namespace)
    old = json.loads(
        run(k + ["-n", "sre-demo", "get", "configmap", "alloy", "-o", "json"]).stdout
    )
    cfg = re.sub(
        r"http://[0-9.]+:3100/loki/api/v1/push",
        "http://" + address("sre-infra-loki-1") + ":3100/loki/api/v1/push",
        old["data"]["config.alloy"],
    )
    logs_changed = cfg != old["data"]["config.alloy"]
    if logs_changed:
        run(
            k
            + [
                "-n",
                "sre-demo",
                "patch",
                "configmap",
                "alloy",
                "--type=merge",
                "--patch-file=/dev/stdin",
            ],
            input=json.dumps({"data": {"config.alloy": cfg}}),
        )
        run(k + ["-n", "sre-demo", "delete", "pods", "-l", "app=alloy", "--wait=false"])
    proof = {
        "actor": "evaluator environment maintenance",
        "no_agent_recovery_claim": True,
        "database_private_addresses_updated": changed,
        "log_address_updated": logs_changed,
        "business_deployment_specs_unchanged": True,
        "started_unix": time.time(),
    }
    output = WORK / ("restart-maintenance-" + str(time.time_ns()) + ".json")
    output.write_text(json.dumps(proof, indent=2))
    for namespace in ("sre-demo", "sre-demo-b"):
        for deployment in ("gateway", "order", "inventory", "probe"):
            run(
                k
                + [
                    "-n",
                    namespace,
                    "rollout",
                    "status",
                    "deployment/" + deployment,
                    "--timeout=120s",
                ],
                timeout=130,
            )
    proof["business"] = {}
    for port in (18084, 18086):
        response = httpx.post(f"http://localhost:{port}/verify", timeout=35)
        response.raise_for_status()
        data = response.json()
        (WORK / f"maintenance-business-{port}.json").write_text(
            json.dumps(data, indent=2)
        )
        proof["business"][str(port)] = data.get("passed") is True
    output.write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof))
    assert all(proof["business"].values()), "Business remains unhealthy"
