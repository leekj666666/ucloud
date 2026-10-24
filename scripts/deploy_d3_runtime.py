"""Start scoped D3 services. Secrets are passed through private files, never printed."""

import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

import psycopg
from cloudsre.store import Store
from cloudsre.worker import database_url
from dotenv import dotenv_values
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "infra/.generated/d3"


def docker(*args, **kwargs):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, **kwargs
    )


def private(path, values):
    path.write_text("".join(k + "=" + v + "\n" for k, v in values.items()))
    path.chmod(0o600)


def replace_container(name, args):
    exists = (
        subprocess.run(
            ["docker", "inspect", name], capture_output=True, check=False
        ).returncode
        == 0
    )
    if exists:
        docker("rm", "-f", name)
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        "sre-infra_default",
        "--user",
        "1000:1000",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=192m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        *args,
    )
    docker("network", "connect", "kind", name)


def main():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    # Administrator only performs schema upgrades and creates a non-superuser runtime role.
    store = Store(database_url())
    store.initialize()
    store.engine.dispose()
    admin = dotenv_values(ROOT / "infra/.env")
    creds = PRIVATE / "worker.env"
    previous = dotenv_values(creds) if creds.exists() else {}
    worker_password = previous.get("WORKER_PASSWORD") or secrets.token_hex(24)
    token = previous.get("CLOUDSRE_DIAG_TOKEN") or secrets.token_hex(24)
    with psycopg.connect(
        host="localhost",
        port=15432,
        user="sre",
        password=admin["POSTGRES_PASSWORD"],
        dbname="sre",
        autocommit=True,
    ) as c:
        if not c.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='sre_worker'"
        ).fetchone():
            c.execute(
                sql.SQL(
                    "CREATE ROLE sre_worker LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Literal(worker_password))
            )
        c.execute("GRANT CONNECT ON DATABASE sre TO sre_worker")
        c.execute("GRANT USAGE ON SCHEMA public TO sre_worker")
        c.execute(
            "GRANT SELECT ON incidents, alert_receipts, workflow_runs, task_ownership, workflow_audit, action_intents TO sre_worker"
        )
        c.execute(
            "GRANT INSERT, UPDATE ON incidents, workflow_runs, task_ownership, workflow_audit, action_intents TO sre_worker"
        )
    work = ROOT / "evaluation/d3-runtime"
    for name in ("diagnoses", "state", "observer"):
        (work / name).mkdir(parents=True, exist_ok=True)
    worker_env = {
        "DATABASE_URL": "postgresql+psycopg://sre_worker:"
        + quote(worker_password, safe="")
        + "@sre-infra-postgres-1:5432/sre",
        "WORKER_PASSWORD": worker_password,
        "CLOUDSRE_DIAG_TOKEN": token,
        "CLOUDSRE_DIAG_URL": "http://cloudsre-diagnosis-d3:8080",
        "CLOUDSRE_DIAG_RESULTS": "/diagnoses",
        "CLOUDSRE_STATE_DIR": "/state",
        "CLOUDSRE_TRUSTED_BASELINE": "/config/baseline.json",
        "CLOUDSRE_EXECUTE_KUBECONFIG": "/config/execute.yaml",
        "CLOUDSRE_VERIFIER_URL": "http://sre-infra-verifier-api-1:8080",
        "CLOUDSRE_PROM_URL": "http://sre-infra-prometheus-1:9090",
        "CLOUDSRE_RESOURCE_SNAPSHOT": "/observations/resources.json",
        "CLOUDSRE_INCIDENT_NOT_BEFORE": os.environ["CLOUDSRE_INCIDENT_NOT_BEFORE"],
    }
    if os.environ.get("CLOUDSRE_TEST_PAUSE_AT"):
        worker_env["CLOUDSRE_TEST_PAUSE_AT"] = os.environ["CLOUDSRE_TEST_PAUSE_AT"]
    for key in ("CLOUDSRE_NAMESPACE", "CLOUDSRE_VERIFIER_B_URL"):
        if os.environ.get(key):
            worker_env[key] = os.environ[key]
    private(creds, worker_env)
    model = dotenv_values("/mnt/d/uclaude/model.env")
    diagnostic_env = {
        k: model[k] for k in ("OPENAI_BASE_URL", "DEEPSEEK_API_KEY", "MODEL_NAME")
    }
    diagnostic_env.update(
        {
            "CLOUDSRE_DIAG_TOKEN": token,
            "CLOUDSRE_DIAG_WORK": "/work",
            "CLOUDSRE_READ_KUBECONFIG": "/config/read.yaml",
            "CLOUDSRE_TRUSTED_BASELINE": "/config/baseline.json",
            "CLOUDSRE_HOLMES_PYTHON": "/usr/local/bin/python",
            "CLOUDSRE_TOOL_PYTHON": "/app/.venv/bin/python",
            "CLOUDSRE_PROM_URL": "http://sre-infra-prometheus-1:9090",
            "CLOUDSRE_LOKI_URL": "http://sre-infra-loki-1:3100",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
    )
    for key in ("CLOUDSRE_DIAG_TIMEOUT_SECONDS", "CLOUDSRE_METRIC_NAMESPACES"):
        if os.environ.get(key):
            diagnostic_env[key] = os.environ[key]
    private(PRIVATE / "diagnosis.env", diagnostic_env)
    replace_container(
        "cloudsre-diagnosis-d3",
        [
            "--memory",
            "768m",
            "--env-file",
            str(PRIVATE / "diagnosis.env"),
            "-p",
            "127.0.0.1:18085:8080",
            "-v",
            str(PRIVATE / "read-container.yaml") + ":/config/read.yaml:ro",
            "-v",
            str(PRIVATE / "baseline.json") + ":/config/baseline.json:ro",
            "-v",
            str(ROOT / ".venv-holmes") + ":" + str(ROOT / ".venv-holmes") + ":ro",
            "-v",
            str(ROOT / "vendor/holmesgpt")
            + ":"
            + str(ROOT / "vendor/holmesgpt")
            + ":ro",
            "-v",
            str(work / "diagnoses") + ":/work:rw",
            "cloudsre-agent:d3",
            "uvicorn",
            "cloudsre.diagnosis_jobs:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--no-access-log",
        ],
    )
    # Worker has no model API key, administrator kubeconfig or Docker socket.
    for index in (1, 2):
        replace_container(
            "cloudsre-worker-d3-" + str(index),
            [
                "--memory",
                "256m",
                "--env-file",
                str(creds),
                "-v",
                str(PRIVATE / "execute-container.yaml") + ":/config/execute.yaml:ro",
                "-v",
                str(PRIVATE / "baseline.json") + ":/config/baseline.json:ro",
                "-v",
                str(work / "diagnoses") + ":/diagnoses:ro",
                "-v",
                str(work / "state") + ":/state:rw",
                "-v",
                str(work / "observer") + ":/observations:ro",
                "cloudsre-agent:d3",
            ],
        )
    print(
        json.dumps(
            {
                "started": [
                    "cloudsre-diagnosis-d3",
                    "cloudsre-worker-d3-1",
                    "cloudsre-worker-d3-2",
                ],
                "runtime_database_role": "sre_worker",
                "secrets_printed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
