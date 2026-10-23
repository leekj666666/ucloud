"""Refresh local Docker/kind bridge addresses after Docker Desktop restart."""

import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PREFIX = [
    str(Path.home() / "sre-tools/bin/kubectl"),
    "--context",
    "kind-sre-dev",
    "-n",
    "sre-demo",
]


def address(name):
    info = json.loads(
        subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                name,
            ],
            text=True,
        )
    )
    return info["sre-infra_default"]["IPAddress"]


def main():
    password = dotenv_values(ROOT / "infra/.env.demo")["DEMO_PASSWORD"]
    url = (
        "postgresql+psycopg://sre_demo:"
        + quote(password, safe="")
        + "@"
        + address("sre-infra-postgres-1")
        + ":5432/sre_demo"
    )
    patch = {"data": {"DATABASE_URL": base64.b64encode(url.encode()).decode()}}
    # Credentials only go through stdin; no command-line values or log output.
    result = subprocess.run(
        PREFIX
        + ["patch", "secret", "demo-db", "--type=merge", "--patch-file=/dev/stdin"],
        input=json.dumps(patch),
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("Private database configuration refresh failed")
    raw = json.loads(
        subprocess.check_output(
            PREFIX + ["get", "configmap", "alloy", "-o", "json"], text=True
        )
    )
    cfg = re.sub(
        r"http://[0-9.]+:3100/loki/api/v1/push",
        "http://" + address("sre-infra-loki-1") + ":3100/loki/api/v1/push",
        raw["data"]["config.alloy"],
    )
    subprocess.run(
        PREFIX
        + [
            "patch",
            "configmap",
            "alloy",
            "--type=merge",
            "-p",
            json.dumps({"data": {"config.alloy": cfg}}),
        ],
        check=True,
    )
    subprocess.run(
        PREFIX
        + [
            "patch",
            "deployment",
            "alloy",
            "--type=merge",
            "-p",
            json.dumps(
                {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "cloudsre/config-hash": hashlib.sha256(
                                        cfg.encode()
                                    ).hexdigest()
                                }
                            }
                        }
                    }
                }
            ),
        ],
        check=True,
    )
    for name in ("inventory", "order", "gateway", "probe"):
        subprocess.run(
            PREFIX + ["rollout", "restart", "deployment/" + name], check=True
        )
    for name in ("inventory", "order", "gateway", "probe", "alloy"):
        subprocess.run(
            PREFIX + ["rollout", "status", "deployment/" + name, "--timeout=120s"],
            check=True,
            timeout=130,
        )
    print("Local bridge configuration refreshed; no Agent recovery claimed")


if __name__ == "__main__":
    main()
