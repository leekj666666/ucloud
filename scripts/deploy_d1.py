"""Configure local Docker/kind development services. Never prints credentials."""

import base64
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

import psycopg
import yaml
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from psycopg import sql
from sqlalchemy import create_engine

ROOT = Path.home() / "sre-project"
INFRA = ROOT / "infra"
os.environ["PATH"] = str(Path.home() / "sre-tools/bin") + ":" + os.environ["PATH"]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def inspect(container):
    return json.loads(subprocess.check_output(["docker", "inspect", container]))[0]


def ip(container):
    return inspect(container)["NetworkSettings"]["Networks"]["sre-infra_default"][
        "IPAddress"
    ]


def private_write(path, content):
    path.write_text(content)
    path.chmod(0o600)


def migrate_business(url):
    cfg = Config()
    cfg.set_main_option(
        "script_location", str(ROOT / "backend/cloudsre/demo_migrations")
    )
    engine = create_engine(url)
    with engine.begin() as c:
        cfg.attributes["connection"] = c
        command.upgrade(cfg, "head")
    engine.dispose()


def configure():
    cfg = dotenv_values(INFRA / ".env")
    demo_file = INFRA / ".env.demo"
    if not demo_file.exists():
        private_write(demo_file, "DEMO_PASSWORD=" + secrets.token_hex(24) + "\n")
    password = dotenv_values(demo_file)["DEMO_PASSWORD"]
    with psycopg.connect(
        host="localhost",
        port=15432,
        user="sre",
        password=cfg["POSTGRES_PASSWORD"],
        dbname="sre",
        autocommit=True,
    ) as c:
        if not c.execute("SELECT 1 FROM pg_roles WHERE rolname='sre_demo'").fetchone():
            c.execute(
                sql.SQL("CREATE ROLE sre_demo LOGIN PASSWORD {}").format(
                    sql.Literal(password)
                )
            )
        if not c.execute(
            "SELECT 1 FROM pg_database WHERE datname='sre_demo'"
        ).fetchone():
            c.execute("CREATE DATABASE sre_demo OWNER sre_demo")
    migrate_business(
        f"postgresql+psycopg://sre_demo:{quote(password, safe='')}@localhost:15432/sre_demo"
    )
    if (
        "sre-infra_default"
        not in inspect("sre-dev-control-plane")["NetworkSettings"]["Networks"]
    ):
        run(
            "docker", "network", "connect", "sre-infra_default", "sre-dev-control-plane"
        )
    run(
        "kind",
        "load",
        "docker-image",
        os.environ.get("SRE_APP_IMAGE", "cloudsre-app:d1-complete"),
        "--name",
        "sre-dev",
    )
    # Resolve and load Alloy from its official image; record immutable ID/digest.
    lock_path = ROOT / "deploy/d1-images.json"
    alloy_ref = (
        json.loads(lock_path.read_text())["alloy"]
        if lock_path.exists()
        else "grafana/alloy:latest"
    )
    if "@sha256:" not in alloy_ref:
        raise RuntimeError("Alloy must be pinned to an immutable digest")
    if subprocess.run(
        ["docker", "image", "inspect", alloy_ref],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode:
        run("docker", "pull", alloy_ref)
    run("docker", "tag", alloy_ref, "cloudsre-alloy:d1")
    alloy = json.loads(
        subprocess.check_output(["docker", "image", "inspect", "cloudsre-alloy:d1"])
    )[0]
    archive = Path.home() / "sre-tools/alloy-amd64.tar"
    run(
        "docker",
        "image",
        "save",
        "--platform=linux/amd64",
        "-o",
        str(archive),
        "cloudsre-alloy:d1",
    )
    run("kind", "load", "image-archive", str(archive), "--name", "sre-dev")
    image_info = {
        "app": json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "image",
                    "inspect",
                    os.environ.get("SRE_APP_IMAGE", "cloudsre-app:d1-complete"),
                ]
            )
        )[0]["Id"],
        "alloy": alloy["RepoDigests"][0],
    }
    (ROOT / "deploy/d1-images.json").write_text(json.dumps(image_info, indent=2))
    namespace = "sre-demo"
    docs = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}]
    dburl = f"postgresql+psycopg://sre_demo:{quote(password, safe='')}@{ip('sre-infra-postgres-1')}:5432/sre_demo"
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "demo-db", "namespace": namespace},
            "type": "Opaque",
            "data": {"DATABASE_URL": base64.b64encode(dburl.encode()).decode()},
        }
    )
    for role, port in [
        ("inventory", 30081),
        ("order", 30080),
        ("gateway", 30082),
        ("probe", 30083),
    ]:
        env = [
            {"name": "SERVICE_ROLE", "value": role},
            {
                "name": "DATABASE_URL",
                "valueFrom": {
                    "secretKeyRef": {"name": "demo-db", "key": "DATABASE_URL"}
                },
            },
            {"name": "RELEASE_ID", "value": "v20261023.1"},
        ]
        if role == "gateway":
            env.append({"name": "ORDER_URL", "value": "http://order:8000"})
        if role == "probe":
            env.extend(
                [
                    {"name": "GATEWAY_URL", "value": "http://gateway:8000"},
                    {"name": "INVENTORY_URL", "value": "http://inventory:8000"},
                    {
                        "name": "CLOUDSRE_VERIFY_DATABASE_URL",
                        "valueFrom": {
                            "secretKeyRef": {"name": "demo-db", "key": "DATABASE_URL"}
                        },
                    },
                ]
            )
        if role == "order":
            env.append({"name": "INVENTORY_URL", "value": "http://inventory:8000"})
        docs.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": role, "namespace": namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": role}},
                    "template": {
                        "metadata": {
                            "labels": {"app": role},
                            "annotations": {"cloudsre/image-id": image_info["app"]},
                        },
                        "spec": {
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": role,
                                    "image": os.environ.get(
                                        "SRE_APP_IMAGE", "cloudsre-app:d1-complete"
                                    ),
                                    "imagePullPolicy": "Never",
                                    "env": env,
                                    "command": (
                                        [
                                            "uvicorn",
                                            "cloudsre.probe:app",
                                            "--host",
                                            "0.0.0.0",
                                            "--port",
                                            "8000",
                                            "--no-access-log",
                                        ]
                                        if role == "probe"
                                        else [
                                            "uvicorn",
                                            "cloudsre.demo_serve:app",
                                            "--host",
                                            "0.0.0.0",
                                            "--port",
                                            "8000",
                                            "--no-access-log",
                                        ]
                                    ),
                                    "ports": [{"containerPort": 8000}],
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "64Mi"},
                                        "limits": {"cpu": "500m", "memory": "192Mi"},
                                    },
                                    "readinessProbe": {
                                        "httpGet": {"path": "/ready", "port": 8000},
                                        "initialDelaySeconds": 2,
                                        "periodSeconds": 3,
                                    },
                                    "livenessProbe": {
                                        "httpGet": {"path": "/health", "port": 8000},
                                        "initialDelaySeconds": 10,
                                        "periodSeconds": 10,
                                    },
                                }
                            ],
                        },
                    },
                },
            }
        )
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": role, "namespace": namespace},
                "spec": {
                    "type": "NodePort",
                    "selector": {"app": role},
                    "ports": [{"port": 8000, "targetPort": 8000, "nodePort": port}],
                },
            }
        )
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "alloy", "namespace": namespace},
        }
    )
    docs.append(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "read-logs", "namespace": namespace},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["get", "list", "watch"],
                },
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            ],
        }
    )
    docs.append(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "alloy-logs", "namespace": namespace},
            "subjects": [
                {"kind": "ServiceAccount", "name": "alloy", "namespace": namespace}
            ],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "read-logs",
            },
        }
    )
    alloy_cfg = """discovery.kubernetes "pods" {
  role = "pod"
  namespaces { names = ["sre-demo"] }
  selectors {
    role = "pod"
    label = "app in (gateway,order,inventory,probe)"
  }
}
discovery.relabel "logs" {
  targets = discovery.kubernetes.pods.targets
  rule {
    source_labels = ["__meta_kubernetes_pod_label_app"]
    target_label = "service"
  }
  rule {
    source_labels = ["__meta_kubernetes_namespace"]
    target_label = "namespace"
  }
}
loki.source.kubernetes "pods" {
  targets = discovery.relabel.logs.output
  forward_to = [loki.write.local.receiver]
}
loki.write "local" {
  endpoint { url = "LOKI_URL/loki/api/v1/push" }
}
""".replace("LOKI_URL", "http://" + ip("sre-infra-loki-1") + ":3100")
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "alloy", "namespace": namespace},
            "data": {"config.alloy": alloy_cfg},
        }
    )
    docs.append(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "alloy", "namespace": namespace},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "alloy"}},
                "template": {
                    "metadata": {
                        "labels": {"app": "alloy"},
                        "annotations": {
                            "cloudsre/config-hash": __import__("hashlib")
                            .sha256(alloy_cfg.encode())
                            .hexdigest()
                        },
                    },
                    "spec": {
                        "serviceAccountName": "alloy",
                        "containers": [
                            {
                                "name": "alloy",
                                "image": "cloudsre-alloy:d1",
                                "imagePullPolicy": "Never",
                                "args": [
                                    "run",
                                    "--storage.path=/tmp/alloy",
                                    "/etc/alloy/config.alloy",
                                ],
                                "resources": {
                                    "requests": {"memory": "64Mi", "cpu": "20m"},
                                    "limits": {"memory": "192Mi", "cpu": "300m"},
                                },
                                "volumeMounts": [
                                    {"name": "config", "mountPath": "/etc/alloy"}
                                ],
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": "alloy"}}],
                    },
                },
            },
        }
    )
    private = INFRA / ".generated"
    private.mkdir(exist_ok=True, mode=0o700)
    manifest = private / "d1.yaml"
    private_write(manifest, yaml.safe_dump_all(docs))
    run(
        "kubectl",
        "--context",
        "kind-sre-dev",
        "apply",
        "-f",
        str(manifest),
        stdout=subprocess.DEVNULL,
    )
    for role in ["inventory", "order", "gateway", "probe", "alloy"]:
        run(
            "kubectl",
            "--context",
            "kind-sre-dev",
            "-n",
            namespace,
            "rollout",
            "status",
            "deployment/" + role,
            "--timeout=180s",
        )
    # Ensure secrets remain in env files and Kubernetes Secrets, not the public manifest.
    public_docs = [d for d in docs if d["kind"] != "Secret"]
    (ROOT / "deploy/d1-kubernetes.yaml").write_text(yaml.safe_dump_all(public_docs))
    compose = yaml.safe_load((INFRA / "compose.yml").read_text())
    incident_url = (
        "postgresql+psycopg://sre:"
        + quote(cfg["POSTGRES_PASSWORD"], safe="")
        + "@postgres:5432/sre"
    )
    if not cfg.get("SRE_DATABASE_URL"):
        with (INFRA / ".env").open("a") as f:
            f.write("SRE_DATABASE_URL=" + incident_url + "\n")
    compose["services"]["incident-api"] = {
        "image": os.environ.get("SRE_APP_IMAGE", "cloudsre-app:d1-complete"),
        "command": [
            "uvicorn",
            "cloudsre.serve:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
        ],
        "environment": {"DATABASE_URL": "${SRE_DATABASE_URL}"},
        "ports": ["127.0.0.1:18080:8080"],
        "mem_limit": "192m",
    }
    compose["services"]["prometheus"]["volumes"].append(
        "./rules.yml:/etc/prometheus/rules.yml:ro"
    ) if "./rules.yml:/etc/prometheus/rules.yml:ro" not in compose["services"][
        "prometheus"
    ]["volumes"] else None

    verifier_evidence = ROOT / "evaluation/verifier-api"
    verifier_evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not cfg.get("SRE_DEMO_DATABASE_URL"):
        with (INFRA / ".env").open("a") as f:
            f.write(
                "SRE_DEMO_DATABASE_URL=postgresql+psycopg://sre_demo:"
                + quote(password, safe="")
                + "@postgres:5432/sre_demo\n"
            )
    compose["services"]["verifier-api"] = {
        "image": os.environ.get("SRE_APP_IMAGE", "cloudsre-app:d1-complete"),
        "command": [
            "uvicorn",
            "cloudsre.verifier_api:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--no-access-log",
        ],
        "user": str(os.getuid()) + ":" + str(os.getgid()),
        "environment": {
            "CLOUDSRE_VERIFY_DATABASE_URL": "${SRE_DEMO_DATABASE_URL}",
            "CLOUDSRE_VERIFY_EVIDENCE_DIR": "/evidence",
            "GATEWAY_URL": "http://sre-dev-control-plane:30082",
            "INVENTORY_URL": "http://sre-dev-control-plane:30081",
        },
        "ports": ["127.0.0.1:18084:8080"],
        "volumes": [str(verifier_evidence) + ":/evidence"],
        "mem_limit": "192m",
    }

    provisioning = INFRA / "grafana-provisioning/datasources"
    provisioning.mkdir(parents=True, exist_ok=True)
    (provisioning / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": 1,
                "datasources": [
                    {
                        "name": "Prometheus",
                        "uid": "prometheus",
                        "type": "prometheus",
                        "url": "http://prometheus:9090",
                        "access": "proxy",
                        "isDefault": True,
                    },
                    {
                        "name": "Loki",
                        "uid": "loki",
                        "type": "loki",
                        "url": "http://loki:3100",
                        "access": "proxy",
                    },
                ],
            }
        )
    )
    mount = "./grafana-provisioning:/etc/grafana/provisioning:ro"
    if mount not in compose["services"]["grafana"]["volumes"]:
        compose["services"]["grafana"]["volumes"].append(mount)
    (INFRA / "compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
    targets = []
    for service, port in [
        ("order", 30080),
        ("inventory", 30081),
        ("gateway", 30082),
        ("probe", 30083),
    ]:
        targets.append(
            {
                "targets": [f"sre-dev-control-plane:{port}"],
                "labels": {"service": service},
            }
        )
    prom = {
        "global": {"scrape_interval": "5s", "evaluation_interval": "5s"},
        "rule_files": ["/etc/prometheus/rules.yml"],
        "scrape_configs": [
            {
                "job_name": "prometheus",
                "static_configs": [{"targets": ["prometheus:9090"]}],
            },
            {"job_name": "demo", "static_configs": targets},
        ],
        "alerting": {
            "alertmanagers": [{"static_configs": [{"targets": ["alertmanager:9093"]}]}]
        },
    }
    (INFRA / "prometheus.yml").write_text(yaml.safe_dump(prom))
    (INFRA / "rules.yml").write_text(
        yaml.safe_dump(
            {
                "groups": [
                    {
                        "name": "demo",
                        "rules": [
                            {
                                "alert": "OrderErrors",
                                "expr": 'sum(rate(sre_http_requests_total{service="order",status=~"5.."}[30s])) > 0.1',
                                "for": "10s",
                                "labels": {
                                    "service": "order",
                                    "severity": "warning",
                                    "environment": "demo",
                                },
                                "annotations": {
                                    "summary": "Order requests are failing"
                                },
                            }
                        ],
                    }
                ]
            }
        )
    )

    rules = yaml.safe_load((INFRA / "rules.yml").read_text())
    extra = [
        ("DemoTargetDown", 'up{job="demo"} == 0', "10s", None),
        ("BusinessProbeFailed", "sre_business_probe_success == 0", "10s", "order"),
        (
            "BusinessProbeStale",
            "time() - sre_business_probe_last_completed_unixtime > 30",
            "10s",
            "order",
        ),
        (
            "GatewayErrors",
            'sum(rate(sre_http_requests_total{service="gateway",status=~"5.."}[30s])) > 0.1',
            "10s",
            "gateway",
        ),
    ]
    for name, expression, duration, service in extra:
        labels = {"environment": "demo", "severity": "warning"}
        if service:
            labels["service"] = service
        rules["groups"][0]["rules"].append(
            {
                "alert": name,
                "expr": expression,
                "for": duration,
                "labels": labels,
                "annotations": {"summary": name},
            }
        )
    (INFRA / "rules.yml").write_text(yaml.safe_dump(rules))

    (INFRA / "alertmanager.yml").write_text(
        yaml.safe_dump(
            {
                "route": {
                    "receiver": "incident-api",
                    "group_by": ["alertname", "service"],
                    "group_wait": "1s",
                    "group_interval": "5s",
                    "repeat_interval": "10m",
                },
                "receivers": [
                    {
                        "name": "incident-api",
                        "webhook_configs": [
                            {
                                "url": "http://incident-api:8080/webhooks/alertmanager",
                                "send_resolved": True,
                            }
                        ],
                    }
                ],
            }
        )
    )
    for p in [
        INFRA / "prometheus.yml",
        INFRA / "rules.yml",
        INFRA / "alertmanager.yml",
        provisioning / "local.yaml",
    ]:
        p.chmod(0o644)
    run("docker", "compose", "up", "-d", cwd=INFRA)
    run("docker", "compose", "restart", "prometheus", "alertmanager", cwd=INFRA)
    print(
        "D1 deployed; credentials not printed. Business and monitoring now require smoke verification."
    )


if __name__ == "__main__":
    configure()
