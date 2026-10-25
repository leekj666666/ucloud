"""Verify actual D4 identities, HTTP boundary and private mount separation."""

import json
import subprocess
import uuid
from pathlib import Path

import httpx
import psycopg
from dotenv import dotenv_values
from kubernetes import client, config
from verify_d3_permissions import status

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "infra/.generated/d4"


def main():
    checks = {}
    cfg = dotenv_values(PRIVATE / "worker.env")
    with psycopg.connect(
        host="localhost",
        port=15432,
        user="sre_worker_d4",
        password=cfg["WORKER_PASSWORD"],
        dbname="sre",
        autocommit=True,
    ) as conn:
        conn.execute("SELECT incident_id FROM incident_controls LIMIT 1 FOR UPDATE")
        checks["worker_can_lock_control_row"] = True
        denied = {
            "cannot_forge_approval": "INSERT INTO plan_decisions SELECT * FROM plan_decisions WHERE false",
            "cannot_rewrite_approval": "UPDATE plan_decisions SET decision='approve' WHERE false",
            "cannot_forge_operator_audit": "INSERT INTO operator_events SELECT * FROM operator_events WHERE false",
            "cannot_switch_execution_mode": "UPDATE incident_controls SET execution_mode='auto' WHERE false",
            "cannot_clear_stop": "UPDATE incident_controls SET stop_requested_at=NULL WHERE false",
            "cannot_delete_workflow": "DELETE FROM workflow_runs WHERE false",
        }
        for name, statement in denied.items():
            try:
                conn.execute(statement)
                checks[name] = False
            except psycopg.errors.InsufficientPrivilege:
                checks[name] = True
    try:
        with psycopg.connect(
            host="localhost",
            port=15432,
            user="sre_worker_d4",
            password=cfg["WORKER_PASSWORD"],
            dbname="sre_demo",
            connect_timeout=5,
        ):
            checks["worker_cannot_connect_business_database"] = False
    except psycopg.OperationalError as exc:
        if 'permission denied for database "sre_demo"' not in str(exc):
            raise
        checks["worker_cannot_connect_business_database"] = True
    for role in ("read", "execute"):
        with config.new_client_from_config(
            config_file=str(PRIVATE / (role + "-host.yaml")), persist_config=False
        ) as api:
            apps, core = client.AppsV1Api(api), client.CoreV1Api(api)
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
            checks[role + "_cannot_read_cluster_scope"] = (
                status(
                    apps.read_namespaced_deployment,
                    name="coredns",
                    namespace="kube-system",
                )
                == 403
            )
            checks[role + "_cannot_patch_unlisted_target"] = (
                status(
                    apps.patch_namespaced_deployment,
                    name="probe" if role == "execute" else "order",
                    namespace="sre-demo",
                    body=[
                        {
                            "op": "test",
                            "path": "/metadata/uid",
                            "value": "does-not-exist",
                        }
                    ],
                    _content_type="application/json-patch+json",
                )
                == 403
            )
    for name in (
        "cloudsre-worker-d4-1",
        "cloudsre-worker-d4-2",
        "cloudsre-diagnosis-d4",
    ):
        info = json.loads(
            subprocess.check_output(["docker", "inspect", name], text=True)
        )[0]
        mounts = [m["Destination"] for m in info["Mounts"]]
        keys = {v.split("=", 1)[0] for v in info["Config"]["Env"]}
        checks[name + "_no_host_admin_or_evaluator_mount"] = not any(
            p in mounts
            for p in (
                "/root/.kube",
                "/app/evaluation",
                "/app/scripts",
                "/var/run/docker.sock",
                "/mnt/d/uclaude/model.env",
            )
        )
        checks[name + "_readonly_nonroot"] = (
            info["HostConfig"]["ReadonlyRootfs"]
            and info["Config"]["User"] == "1000:1000"
        )
        if "worker" in name:
            checks[name + "_no_model_key"] = (
                not {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"} & keys
            )
        else:
            checks["diagnosis_has_no_executor_identity"] = (
                "/config/execute.yaml" not in mounts
                and "CLOUDSRE_EXECUTE_KUBECONFIG" not in keys
            )
    token = dotenv_values(PRIVATE / "console.env")["CLOUDSRE_CONSOLE_TOKEN"]
    with httpx.Client(base_url="http://localhost:18080", timeout=20) as http:
        checks["unauthenticated_console_refused"] = (
            http.get("/console/incidents").status_code == 401
        )
        checks["cross_origin_login_refused"] = (
            http.post(
                "/console/session",
                json={"token": token},
                headers={
                    "X-CloudSRE-Request": "console",
                    "Origin": "https://foreign.example",
                },
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/console/session",
                json={"token": token},
                headers={"X-CloudSRE-Request": "console"},
            ).status_code
            == 200
        )
        rows = http.get("/console/incidents?scope=console").json()["items"]
        assert rows
        ident = rows[0]["id"]
        response = http.get(f"/console/incidents/{ident}/export?format=json")
        checks["authenticated_export"] = response.status_code == 200
        secrets = [token, cfg["WORKER_PASSWORD"], cfg["CLOUDSRE_DIAG_TOKEN"]]
        secrets += [
            v
            for k, v in dotenv_values("/mnt/d/uclaude/model.env").items()
            if k.endswith("API_KEY") and v
        ]
        checks["no_runtime_credentials_in_export"] = not any(
            value in response.text for value in secrets
        )
        checks["cross_origin_stop_refused"] = (
            http.post(
                f"/console/incidents/{ident}/stop",
                json={"reason": "must be refused"},
                headers={
                    "X-CloudSRE-Request": "console",
                    "Origin": "https://foreign.example",
                },
            ).status_code
            == 403
        )
        checks["arbitrary_evidence_path_refused"] = http.get(
            f"/console/incidents/{ident}/evidence/..%2F..%2Fmodel.env"
        ).status_code in (400, 404, 422)
        checks["security_headers_present"] = (
            response.headers.get("cache-control") == "no-store"
            and response.headers.get("x-content-type-options") == "nosniff"
        )
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "model_calls": 0,
        "scope": "Actual deployed D4 containers, restricted DB roles, scoped Kubernetes identities and HTTP routes",
    }
    folder = ROOT / "evaluation/runs" / ("d4-permissions-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    result["folder"] = str(folder)
    (ROOT / "evaluation/d4-permissions.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    assert result["passed"]


if __name__ == "__main__":
    main()
