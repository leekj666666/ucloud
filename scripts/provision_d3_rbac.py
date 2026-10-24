"""Provision scoped identities; kubeconfig/token output is never printed."""

import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KUBECTL = str(Path.home() / "sre-tools/bin/kubectl")
PREFIX = [KUBECTL, "--context", "kind-sre-dev"]
OUT = ROOT / "infra/.generated/d3"
OUT.mkdir(parents=True, exist_ok=True)


def run(args, **kwargs):
    return subprocess.run(
        PREFIX + args, check=True, capture_output=True, text=True, **kwargs
    )


def main():
    cfg = json.loads(run(["config", "view", "--raw", "-o", "json"]).stdout)
    context = next(c["context"] for c in cfg["contexts"] if c["name"] == "kind-sre-dev")
    cluster = next(
        c["cluster"] for c in cfg["clusters"] if c["name"] == context["cluster"]
    )
    ca = cluster["certificate-authority-data"]
    for namespace in ("sre-demo", "sre-demo-b"):
        docs = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}
        ]
        for role in ("read", "execute"):
            name = "cloudsre-" + role
            docs.append(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": name, "namespace": namespace},
                    "automountServiceAccountToken": False,
                }
            )
            if role == "read":
                rules = [
                    {
                        "apiGroups": ["apps"],
                        "resources": ["deployments", "replicasets"],
                        "verbs": ["get", "list"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["services", "pods", "events"],
                        "verbs": ["get", "list"],
                    },
                    {
                        "apiGroups": ["discovery.k8s.io"],
                        "resources": ["endpointslices"],
                        "verbs": ["get", "list"],
                    },
                ]
            else:
                rules = [
                    {
                        "apiGroups": ["apps"],
                        "resources": ["deployments"],
                        "resourceNames": ["order", "inventory", "gateway"],
                        "verbs": ["get", "patch"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["services"],
                        "resourceNames": ["order", "inventory", "gateway"],
                        "verbs": ["get", "patch"],
                    },
                ]
            docs.extend(
                [
                    {
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "kind": "Role",
                        "metadata": {"name": name, "namespace": namespace},
                        "rules": rules,
                    },
                    {
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "kind": "RoleBinding",
                        "metadata": {"name": name, "namespace": namespace},
                        "subjects": [
                            {
                                "kind": "ServiceAccount",
                                "name": name,
                                "namespace": "sre-demo",
                            }
                        ],
                        "roleRef": {
                            "apiGroup": "rbac.authorization.k8s.io",
                            "kind": "Role",
                            "name": name,
                        },
                    },
                ]
            )
        run(["apply", "-f", "-"], input=yaml.safe_dump_all(docs))
    for role in ("read", "execute"):
        token = run(
            ["-n", "sre-demo", "create", "token", "cloudsre-" + role, "--duration=24h"]
        ).stdout.strip()
        for mode, server in (
            ("host", cluster["server"]),
            ("container", "https://sre-dev-control-plane:6443"),
        ):
            data = {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {
                        "name": "local",
                        "cluster": {
                            "server": server,
                            "certificate-authority-data": ca,
                            **(
                                {"tls-server-name": "kubernetes"}
                                if mode == "container"
                                else {}
                            ),
                        },
                    }
                ],
                "users": [{"name": role, "user": {"token": token}}],
                "contexts": [
                    {
                        "name": role,
                        "context": {
                            "cluster": "local",
                            "user": role,
                            "namespace": "sre-demo",
                        },
                    }
                ],
                "current-context": role,
            }
            p = OUT / (role + "-" + mode + ".yaml")
            p.write_text(yaml.safe_dump(data))
            p.chmod(0o600)
    print(
        "Scoped read/execute identities provisioned; token lifetime requested: 24h. No credentials printed."
    )


if __name__ == "__main__":
    main()
