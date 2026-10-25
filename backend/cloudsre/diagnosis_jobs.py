"""Idempotent asynchronous diagnosis jobs, isolated from execution and evaluation."""

import hmac
import json
import os
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from .durable import digest, now
from .policy_v3 import parse

ROOT = Path(os.environ.get("CLOUDSRE_DIAG_WORK", "/work"))
ROOT.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
PROCESSES = {}
CANCELLATIONS = set()
POOL = ThreadPoolExecutor(max_workers=2)
app = FastAPI(title="Read-only diagnostic jobs")


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident: dict = Field()
    # Caller supplies sanitized incident and a time window, never expected fault/action.


def authenticate(token):
    if not hmac.compare_digest(token or "", os.environ["CLOUDSRE_DIAG_TOKEN"]):
        raise HTTPException(403, "Invalid diagnostic client")


def directory(ident):
    if not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise HTTPException(400, "Invalid job ID")
    return ROOT / ident


def write(path, body):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False))
    tmp.replace(path)


def execute(folder):
    status = json.loads((folder / "status.json").read_text())
    try:
        env = {
            **os.environ,
            "CLOUDSRE_RUN_DIR": str(folder),
            "PYTHONPATH": "/app/backend:/home/lee/sre-project/.venv-holmes/lib/python3.12/site-packages:/home/lee/sre-project/vendor/holmesgpt",
        }
        key = os.environ["DEEPSEEK_API_KEY"]
        timeout_seconds = int(os.environ.get("CLOUDSRE_DIAG_TIMEOUT_SECONDS", "180"))
        if not 1 <= timeout_seconds <= 180:
            raise ValueError("Diagnostic timeout must be within 1..180 seconds")
        with LOCK:
            if status["id"] in CANCELLATIONS:
                write(
                    folder / "status.json",
                    {**status, "state": "cancelled", "ended_at": now()},
                )
                return
            p = subprocess.Popen(
                [
                    os.environ["CLOUDSRE_HOLMES_PYTHON"],
                    os.environ.get(
                        "CLOUDSRE_DIAGNOSTIC_RUNNER", "/app/diagnosis/runner_v3.py"
                    ),
                    str(folder),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            PROCESSES[status["id"]] = p
        try:
            stdout, stderr = p.communicate(timeout=timeout_seconds)
            state = (
                "complete"
                if p.returncode == 0 and (folder / "holmes-result.json").exists()
                else "failed"
            )
        except subprocess.TimeoutExpired:
            terminate_group(p)
            stdout, stderr = p.communicate()
            state = "timeout"
        output = stdout + "\n" + stderr
        with LOCK:
            if status["id"] in CANCELLATIONS:
                state = "cancelled"
        validation = {}
        if state == "complete":
            try:
                parsed = json.loads((folder / "holmes-result.json").read_text())
                parse(parsed["result"])
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                state = "failed"
                validation = {
                    "error_kind": "invalid_model_output",
                    "validation_error": type(exc).__name__
                    + ": return one JSON object using the documented action-specific scalar/object value type",
                }
        (folder / "process.log").write_text(output.replace(key, "[REDACTED]"))
        with LOCK:
            if status["id"] in CANCELLATIONS:
                state = "cancelled"
            write(
                folder / "status.json",
                {**status, **validation, "state": state, "ended_at": now()},
            )
            PROCESSES.pop(status["id"], None)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        with LOCK:
            write(
                folder / "status.json",
                {
                    **status,
                    "state": "cancelled" if status["id"] in CANCELLATIONS else "failed",
                    "error_type": type(exc).__name__,
                    "ended_at": now(),
                },
            )

    finally:
        with LOCK:
            process = PROCESSES.pop(status["id"], None)
        if process is not None and process.poll() is None:
            terminate_group(process)
            process.wait(timeout=5)


def terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# Restarted service must not pretend an orphaned process is still making progress.
for path in ROOT.glob("*/status.json"):
    state = json.loads(path.read_text())
    if state["state"] == "running":
        write(path, {**state, "state": "interrupted", "ended_at": now()})


@app.post("/jobs/{ident}")
def start(
    ident: str, data: JobInput, x_diagnostic_token: str | None = Header(default=None)
):
    authenticate(x_diagnostic_token)
    folder = directory(ident)
    request_hash = digest(data.incident)
    with LOCK:
        if (folder / "status.json").exists():
            old = json.loads((folder / "status.json").read_text())
            if old["request_hash"] != request_hash:
                raise HTTPException(409, "Job identity reused with different input")
            return old
        folder.mkdir()
        write(folder / "incident.json", data.incident)
        status = {
            "id": ident,
            "request_hash": request_hash,
            "state": "running",
            "started_at": now(),
            "model": os.environ.get("MODEL_NAME"),
            "max_output_tokens": 8192,
            "max_steps": 8,
            "timeout_seconds": int(
                os.environ.get("CLOUDSRE_DIAG_TIMEOUT_SECONDS", "180")
            ),
        }
        write(folder / "status.json", status)
        POOL.submit(execute, folder)
        return status


@app.get("/jobs/{ident}")
def get(ident: str, x_diagnostic_token: str | None = Header(default=None)):
    authenticate(x_diagnostic_token)
    path = directory(ident) / "status.json"
    if not path.exists():
        raise HTTPException(404, "Unknown job")
    return json.loads(path.read_text())


@app.post("/jobs/{ident}/cancel")
def cancel(ident: str, x_diagnostic_token: str | None = Header(default=None)):
    authenticate(x_diagnostic_token)
    folder = directory(ident)
    with LOCK:
        CANCELLATIONS.add(ident)
        if not (folder / "status.json").exists():
            # Tombstone fences a delayed create request with this same job ID.
            folder.mkdir(exist_ok=True)
            state = {
                "id": ident,
                "request_hash": None,
                "state": "cancelled",
                "started_at": None,
                "ended_at": now(),
                "reason": "cancelled_before_submission",
            }
            write(folder / "status.json", state)
            return state
        state = json.loads((folder / "status.json").read_text())
        if state["state"] not in {"running", "cancelling"}:
            return state
        process = PROCESSES.get(ident)
        if process is None:
            write(
                folder / "status.json",
                {**state, "state": "cancelled", "ended_at": now()},
            )
        else:
            write(folder / "status.json", {**state, "state": "cancelling"})
            terminate_group(process)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = json.loads((folder / "status.json").read_text())
        if state["state"] not in {"running", "cancelling"}:
            return state
        time.sleep(0.05)
    return state


@app.get("/health")
def health():
    return {"status": "ok", "role": "read-only-diagnosis"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    from .resource_metrics import render

    return render(os.environ.get("CLOUDSRE_METRIC_NAMESPACES", "sre-demo").split(","))
