"""Evaluator-only preparation of a second, independently addressed business graph."""

import copy
import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KUBE = [str(Path.home() / "sre-tools/bin/kubectl"), "--context", "kind-sre-dev"]


def apply(obj):
    result = subprocess.run(
        KUBE + ["apply", "-f", "-"],
        input=json.dumps(obj),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Scoped parallel environment apply failed")


def main():
    subprocess.run(
        [str(ROOT / ".venv/bin/python"), "scripts/build_d3_manifest.py"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "build",
            "-t",
            "cloudsre-agent:d3",
            "-f",
            "deploy/Dockerfile.d3",
            ".",
        ],
        cwd=ROOT,
        check=True,
    )
    apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "sre-demo-b"}})
    for kind, names in (
        ("Secret", ["demo-db"]),
        ("Deployment", ["gateway", "order", "inventory", "probe"]),
        ("Service", ["gateway", "order", "inventory", "probe"]),
    ):
        for name in names:
            raw = json.loads(
                subprocess.check_output(
                    KUBE + ["-n", "sre-demo", "get", kind, name, "-o", "json"],
                    text=True,
                )
            )
            raw.pop("status", None)
            raw["metadata"] = {
                "name": name,
                "namespace": "sre-demo-b",
                "labels": raw["metadata"].get("labels", {}),
            }
            if kind == "Service":
                for field in (
                    "clusterIP",
                    "clusterIPs",
                    "ipFamilies",
                    "ipFamilyPolicy",
                    "healthCheckNodePort",
                ):
                    raw["spec"].pop(field, None)
                for port in raw["spec"]["ports"]:
                    if "nodePort" in port:
                        port["nodePort"] = {
                            "order": 30090,
                            "inventory": 30091,
                            "gateway": 30092,
                            "probe": 30093,
                        }[name]
            if kind == "Deployment":
                raw["spec"]["replicas"] = 1
            apply(raw)
    for name in ("gateway", "order", "inventory", "probe"):
        subprocess.run(
            KUBE
            + [
                "-n",
                "sre-demo-b",
                "rollout",
                "status",
                "deployment/" + name,
                "--timeout=120s",
            ],
            check=True,
        )
    source = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "sre-infra-verifier-api-1"], text=True
        )
    )[0]
    env = dict(x.split("=", 1) for x in source["Config"]["Env"])
    private = ROOT / "infra/.generated/d3/verifier-b.env"
    values = {
        key: env[key]
        for key in ("CLOUDSRE_VERIFY_DATABASE_URL", "CLOUDSRE_VERIFY_EVIDENCE_DIR")
    }
    values.update(
        GATEWAY_URL="http://sre-dev-control-plane:30092",
        INVENTORY_URL="http://sre-dev-control-plane:30091",
    )
    private.write_text("".join(k + "=" + v + "\n" for k, v in values.items()))
    private.chmod(0o600)
    folder = ROOT / "evaluation/verifier-b"
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["docker", "rm", "-f", "cloudsre-verifier-b"], capture_output=True, check=False
    )
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "cloudsre-verifier-b",
            "--network",
            "sre-infra_default",
            "--user",
            "1000:1000",
            "--memory",
            "192m",
            "--env-file",
            str(private),
            "-p",
            "127.0.0.1:18086:8080",
            "-v",
            str(folder) + ":/evidence",
            source["Image"],
            *source["Config"]["Cmd"],
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["docker", "network", "connect", "kind", "cloudsre-verifier-b"], check=True
    )
    path = ROOT / "infra/prometheus.yml"
    cfg = yaml.safe_load(path.read_text())
    first = next(x for x in cfg["scrape_configs"] if x["job_name"] == "demo")
    for target in first["static_configs"]:
        target["labels"]["namespace"] = "sre-demo"
    second = copy.deepcopy(first)
    second["job_name"] = "demo-b"
    for target in second["static_configs"]:
        target["labels"]["namespace"] = "sre-demo-b"
        target["targets"] = [t.replace(":3008", ":3009") for t in target["targets"]]
    cfg["scrape_configs"] = [
        x for x in cfg["scrape_configs"] if x["job_name"] != "demo-b"
    ] + [second]
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    path = ROOT / "infra/rules.yml"
    rules = yaml.safe_load(path.read_text())
    for group in rules["groups"]:
        for rule in group["rules"]:
            if rule["alert"] in {"OrderErrors", "GatewayErrors"}:
                rule["expr"] = rule["expr"].replace("sum(", "sum by(namespace)(", 1)
            if rule["alert"] == "DemoTargetDown":
                rule["expr"] = 'up{job=~"demo|demo-b"} == 0'
    path.write_text(yaml.safe_dump(rules, sort_keys=False))
    subprocess.run(["docker", "restart", "sre-infra-prometheus-1"], check=True)
    print(
        "Second graph and namespace-separated monitoring configured; no credentials printed"
    )


if __name__ == "__main__":
    main()
