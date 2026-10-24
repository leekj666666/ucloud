"""Actual allowed/denied Kubernetes calls plus container filesystem boundaries."""

import json
import subprocess
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "infra/.generated/d3"


def status(fn, **args):
    try:
        r = fn(**args, _preload_content=False, _request_timeout=(5, 15))
        try:
            return r.status
        finally:
            r.release_conn()
    except ApiException as exc:
        return exc.status


def main():
    checks = {}
    for role in ("read", "execute"):
        with config.new_client_from_config(
            config_file=str(PRIVATE / (role + "-host.yaml")), persist_config=False
        ) as api:
            apps = client.AppsV1Api(api)
            core = client.CoreV1Api(api)
            checks[role + "_can_read_order"] = (
                status(
                    apps.read_namespaced_deployment, name="order", namespace="sre-demo"
                )
                == 200
            )
            checks[role + "_cannot_read_secret"] = (
                status(
                    core.read_namespaced_secret, name="demo-db", namespace="sre-demo"
                )
                == 403
            )
            checks[role + "_cannot_read_other_namespace"] = (
                status(
                    apps.read_namespaced_deployment,
                    name="coredns",
                    namespace="kube-system",
                )
                == 403
            )
            if role == "read":
                checks["diagnostic_cannot_patch"] = (
                    status(
                        apps.patch_namespaced_deployment,
                        name="order",
                        namespace="sre-demo",
                        body=[
                            {
                                "op": "test",
                                "path": "/metadata/uid",
                                "value": "nonexistent",
                            }
                        ],
                        _content_type="application/json-patch+json",
                    )
                    == 403
                )
            else:
                checks["executor_cannot_patch_unlisted_target"] = (
                    status(
                        apps.patch_namespaced_deployment,
                        name="probe",
                        namespace="sre-demo",
                        body=[
                            {
                                "op": "test",
                                "path": "/metadata/uid",
                                "value": "nonexistent",
                            }
                        ],
                        _content_type="application/json-patch+json",
                    )
                    == 403
                )
    # Only the scoped credential is mounted, never the host kubeconfig or repo.
    code = """import os
from pathlib import Path
from cloudsre.kube_gateway import KubeGateway
g=KubeGateway('read')
assert g.read('sre-demo/Deployment/order')['metadata']['name']=='order'
g.close()
for p in ['/app/scripts','/app/evaluation','/root/.kube/config','/var/run/docker.sock','/mnt/d/uclaude/model.env']:
 try:
  assert not Path(p).exists(),p
 except PermissionError:
  pass
print('container_read_and_filesystem_isolation_passed')
"""
    p = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "kind",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            str(PRIVATE / "read-container.yaml") + ":/config/read.yaml:ro",
            "-e",
            "CLOUDSRE_READ_KUBECONFIG=/config/read.yaml",
            "--entrypoint",
            "python",
            "cloudsre-agent:d3",
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    checks["container_read_and_filesystem_isolation"] = p.returncode == 0
    result = {
        "checks": checks,
        "passed": all(checks.values()),
        "model_calls": 0,
        "container_result": p.stdout,
        "container_error": p.stderr[-1500:] if p.returncode else None,
    }
    out = ROOT / "evaluation/d3-permissions.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    assert result["passed"]


if __name__ == "__main__":
    main()
