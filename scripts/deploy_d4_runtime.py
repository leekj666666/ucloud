"""Build and run the local operator console with private server-side credentials."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import psycopg
import yaml
from cloudsre.durable import now
from cloudsre.store import Store
from cloudsre.worker import database_url
from deploy_d3_runtime import docker, private, replace_container
from dotenv import dotenv_values
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "infra/.generated/d4"
WORK = ROOT / "evaluation/d4-runtime"
IMAGE = "cloudsre-agent:d4"
NODE = Path(
    os.environ.get(
        "CLOUDSRE_NODE_BIN", str(Path.home() / "sre-tools/node-v24.22.1-linux-x64/bin")
    )
)


def run(args, **kwargs):
    kwargs.setdefault("cwd", ROOT)
    return subprocess.run(args, check=True, **kwargs)


def replace_worker(name, args, *, start):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    docker(
        "create",
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
    if start:
        docker("start", name)


def observer():
    pidfile = WORK / "observer.pid"
    if pidfile.exists():
        proc = Path("/proc") / pidfile.read_text().strip() / "cmdline"
        if proc.exists() and b"observe_d3_resources.py" in proc.read_bytes():
            return
    with (WORK / "observer.log").open("a") as log:
        p = subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "scripts/observe_d3_resources.py"],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": "backend",
                "CLOUDSRE_OBSERVER_OUTPUT": str(WORK / "observer/resources.json"),
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pidfile.write_text(str(p.pid))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-workers", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    for name in ("state", "diagnoses", "observer"):
        (WORK / name).mkdir(parents=True, exist_ok=True)
    if not args.skip_build:
        run(
            [str(NODE / "npm"), "ci", "--no-audit", "--no-fund"],
            env={**os.environ, "PATH": str(NODE) + ":" + os.environ["PATH"]},
            cwd=ROOT / "frontend",
        )
        run(
            [str(NODE / "npm"), "run", "build"],
            env={**os.environ, "PATH": str(NODE) + ":" + os.environ["PATH"]},
            cwd=ROOT / "frontend",
        )
        run([str(ROOT / ".venv/bin/python"), "scripts/build_d3_manifest.py"])
        run(["docker", "build", "-t", IMAGE, "-f", "deploy/Dockerfile.d4", "."])
    # Avoid replacing running jobs without first preserving their durable state.
    for name in (
        "cloudsre-worker-d4-1",
        "cloudsre-worker-d4-2",
        "cloudsre-diagnosis-d4",
    ):
        subprocess.run(["docker", "stop", name], capture_output=True, check=False)
    store = Store(database_url())
    store.initialize()
    store.engine.dispose()
    admin = dotenv_values(ROOT / "infra/.env")
    old = (
        dotenv_values(PRIVATE / "worker.env")
        if (PRIVATE / "worker.env").exists()
        else {}
    )
    password = old.get("WORKER_PASSWORD") or secrets.token_hex(24)
    diag_token = old.get("CLOUDSRE_DIAG_TOKEN") or secrets.token_hex(24)
    with psycopg.connect(
        host="localhost",
        port=15432,
        user="sre",
        password=admin["POSTGRES_PASSWORD"],
        dbname="sre",
        autocommit=True,
    ) as c:
        role = sql.Identifier("sre_worker_d4")
        exists = c.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='sre_worker_d4'"
        ).fetchone()
        if not exists:
            c.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(role, sql.Literal(password))
            )
        elif not old:
            c.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(role, sql.Literal(password))
            )
        for statement in (
            "GRANT CONNECT ON DATABASE sre TO {}",
            "GRANT USAGE ON SCHEMA public TO {}",
            "GRANT SELECT ON incidents,alert_receipts,workflow_runs,task_ownership,workflow_audit,action_intents,incident_controls,plan_decisions TO {}",
            "GRANT INSERT,UPDATE ON incidents,workflow_runs,task_ownership,workflow_audit,action_intents TO {}",
            "REVOKE UPDATE ON incident_controls FROM {}",
            "GRANT UPDATE (requested_at) ON incident_controls TO {}",
        ):
            c.execute(sql.SQL(statement).format(role))
        c.execute("REVOKE CONNECT ON DATABASE sre_demo FROM PUBLIC")
        c.execute("GRANT CONNECT ON DATABASE sre_demo TO sre_demo, sre")
    source_private = ROOT / "infra/.generated/d3"
    for name in (
        "read-container.yaml",
        "execute-container.yaml",
        "read-host.yaml",
        "execute-host.yaml",
    ):
        shutil.copyfile(source_private / name, PRIVATE / name)
        (PRIVATE / name).chmod(0o600)
    if not (PRIVATE / "baseline.json").exists():
        shutil.copyfile(source_private / "baseline.json", PRIVATE / "baseline.json")
    (PRIVATE / "baseline.json").chmod(0o644)
    worker = {
        "DATABASE_URL": "postgresql+psycopg://sre_worker_d4:"
        + quote(password, safe="")
        + "@sre-infra-postgres-1:5432/sre",
        "WORKER_PASSWORD": password,
        "CLOUDSRE_DIAG_TOKEN": diag_token,
        "CLOUDSRE_DIAG_URL": "http://cloudsre-diagnosis-d4:8080",
        "CLOUDSRE_DIAG_RESULTS": "/diagnoses",
        "CLOUDSRE_STATE_DIR": "/state",
        "CLOUDSRE_TRUSTED_BASELINE": "/config/baseline.json",
        "CLOUDSRE_EXECUTE_KUBECONFIG": "/config/execute.yaml",
        "CLOUDSRE_VERIFIER_URL": "http://sre-infra-verifier-api-1:8080",
        "CLOUDSRE_VERIFIER_B_URL": "http://cloudsre-verifier-b:8080",
        "CLOUDSRE_NAMESPACE": "sre-demo",
        "CLOUDSRE_PROM_URL": "http://sre-infra-prometheus-1:9090",
        "CLOUDSRE_RESOURCE_SNAPSHOT": "/observations/resources.json",
        "CLOUDSRE_OPERATOR_CONTROL": "1",
    }
    private(PRIVATE / "worker.env", worker)
    model = dotenv_values("/mnt/d/uclaude/model.env")
    diagnostic = {
        k: model[k] for k in ("OPENAI_BASE_URL", "DEEPSEEK_API_KEY", "MODEL_NAME")
    }
    routing = os.environ.get("CLOUDSRE_ROUTING_MODE", "h5")
    if routing not in {"h5", "b3"}:
        raise ValueError("CLOUDSRE_ROUTING_MODE must be h5 or b3")
    diagnostic.update(
        CLOUDSRE_DIAGNOSTIC_RUNNER=os.environ.get(
            "CLOUDSRE_DIAGNOSTIC_RUNNER", "/app/diagnosis/runner_h5.py"
        ),
        CLOUDSRE_ROUTING_MODE=routing,
        CLOUDSRE_DIAG_TOKEN=diag_token,
        CLOUDSRE_DIAG_WORK="/work",
        CLOUDSRE_READ_KUBECONFIG="/config/read.yaml",
        CLOUDSRE_TRUSTED_BASELINE="/config/baseline.json",
        CLOUDSRE_HOLMES_PYTHON="/usr/local/bin/python",
        CLOUDSRE_TOOL_PYTHON="/app/.venv/bin/python",
        CLOUDSRE_PROM_URL="http://sre-infra-prometheus-1:9090",
        CLOUDSRE_LOKI_URL="http://sre-infra-loki-1:3100",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        CLOUDSRE_METRIC_NAMESPACES="sre-demo,sre-demo-b",
    )
    private(PRIVATE / "diagnosis.env", diagnostic)
    console_file = PRIVATE / "console.env"
    console_token = (
        dotenv_values(console_file).get("CLOUDSRE_CONSOLE_TOKEN")
        if console_file.exists()
        else None
    )
    console_token = console_token or secrets.token_urlsafe(32)
    private(console_file, {"CLOUDSRE_CONSOLE_TOKEN": console_token})
    access = Path("/mnt/d/uclaude/console-access.txt")
    access.write_text(
        "云巡本机控制台：http://localhost:18080\n访问码："
        + console_token
        + "\n此访问码不是模型 API Key，请勿提交到仓库或报告。\n"
    )
    access.chmod(0o600)
    override = {
        "services": {
            "incident-api": {
                "image": IMAGE,
                "env_file": [str(console_file)],
                "environment": {
                    "CLOUDSRE_CONSOLE_EVIDENCE_ROOTS": json.dumps(
                        ["/data/d4-state", "/data/d3-state", "/data/runs"]
                    )
                },
                "volumes": [
                    str(WORK / "state") + ":/data/d4-state:ro",
                    str(ROOT / "evaluation/d3-runtime/state") + ":/data/d3-state:ro",
                    str(ROOT / "evaluation/runs") + ":/data/runs:ro",
                ],
            }
        }
    }
    override_file = PRIVATE / "console-compose.yml"
    override_file.write_text(yaml.safe_dump(override))
    run(
        [
            "docker",
            "compose",
            "--env-file",
            "infra/.env",
            "-f",
            "infra/compose.yml",
            "-f",
            str(override_file),
            "up",
            "-d",
            "--no-deps",
            "incident-api",
        ]
    )
    observer()
    replace_container(
        "cloudsre-diagnosis-d4",
        [
            "--memory",
            "768m",
            "--env-file",
            str(PRIVATE / "diagnosis.env"),
            "-p",
            "127.0.0.1:18088:8080",
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
            str(WORK / "diagnoses") + ":/work:rw",
            IMAGE,
            "uvicorn",
            "cloudsre.diagnosis_jobs:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--no-access-log",
        ],
    )
    for index in (1, 2):
        name = "cloudsre-worker-d4-" + str(index)
        replace_worker(
            name,
            [
                "--memory",
                "256m",
                "--env-file",
                str(PRIVATE / "worker.env"),
                "-v",
                str(PRIVATE / "execute-container.yaml") + ":/config/execute.yaml:ro",
                "-v",
                str(PRIVATE / "baseline.json") + ":/config/baseline.json:ro",
                "-v",
                str(WORK / "diagnoses") + ":/diagnoses:ro",
                "-v",
                str(WORK / "state") + ":/state:rw",
                "-v",
                str(WORK / "observer") + ":/observations:ro",
                IMAGE,
            ],
            start=not args.no_workers,
        )
    (WORK / "deployment.json").write_text(
        json.dumps(
            {
                "at": now(),
                "image": IMAGE,
                "workers_started": not args.no_workers,
                "operator_control": True,
                "console_url": "http://localhost:18080",
                "credentials_printed": False,
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "console": "http://localhost:18080",
                "access_code_file": "D:/uclaude/console-access.txt",
                "workers_started": not args.no_workers,
                "credentials_printed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
