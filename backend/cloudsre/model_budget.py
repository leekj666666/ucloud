"""Durable upstream HTTP budget. Diagnostic clients never receive the provider key."""

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response


def now():
    return datetime.now(UTC).isoformat()


class BudgetDenied(ValueError):
    pass


class InvalidRunToken(ValueError):
    pass


class ModelBudget:
    def __init__(self, path, limit=100):
        if not 1 <= limit <= 100:
            raise ValueError("This authorized batch is capped at 100 upstream attempts")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), max_attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, max_attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), reserved_at TEXT NOT NULL, request_sha256 TEXT NOT NULL, state TEXT NOT NULL, http_status INTEGER, usage TEXT, error_type TEXT, elapsed_seconds REAL);
                CREATE TABLE IF NOT EXISTS attempt_metadata (id TEXT PRIMARY KEY REFERENCES attempts(id), settings TEXT NOT NULL);
            """)
            conn.execute("INSERT OR IGNORE INTO budget VALUES (1, ?)", (limit,))
            stored = conn.execute(
                "SELECT max_attempts FROM budget WHERE id=1"
            ).fetchone()[0]
            if stored != limit:
                raise ValueError("Persisted batch limit is immutable")
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def register(self, run_id, max_attempts=4):
        if not run_id or not 1 <= max_attempts <= 8:
            raise ValueError("Invalid run budget")
        token = secrets.token_urlsafe(32)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO runs VALUES (?,?,?)",
                (run_id, hashlib.sha256(token.encode()).hexdigest(), max_attempts),
            )
        return token

    def reserve(self, token, body):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run = conn.execute(
                    "SELECT * FROM runs WHERE token_hash=?", (token_hash,)
                ).fetchone()
                if not run:
                    raise InvalidRunToken("Unknown diagnostic run token")
                used = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
                limit = conn.execute(
                    "SELECT max_attempts FROM budget WHERE id=1"
                ).fetchone()[0]
                own = conn.execute(
                    "SELECT count(*) FROM attempts WHERE run_id=?", (run["id"],)
                ).fetchone()[0]
                if used >= limit or own >= run["max_attempts"]:
                    raise BudgetDenied("Upstream model HTTP budget exhausted")
                ident = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO attempts (id,run_id,reserved_at,request_sha256,state) VALUES (?,?,?,?,?)",
                    (
                        ident,
                        run["id"],
                        now(),
                        hashlib.sha256(body).hexdigest(),
                        "reserved_unknown",
                    ),
                )
                try:
                    supplied = json.loads(body)
                except ValueError:
                    supplied = {}
                settings = {
                    key: supplied[key]
                    for key in (
                        "model",
                        "temperature",
                        "top_p",
                        "max_tokens",
                        "max_completion_tokens",
                        "thinking",
                        "reasoning_effort",
                        "stream",
                    )
                    if key in supplied
                }
                conn.execute(
                    "INSERT INTO attempt_metadata VALUES (?,?)",
                    (ident, json.dumps(settings)),
                )
                conn.execute("COMMIT")
                return ident
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def finish(self, ident, *, status=None, usage=None, error_type=None, elapsed=0):
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE attempts SET state='finished',http_status=?,usage=?,error_type=?,elapsed_seconds=? WHERE id=? AND state='reserved_unknown'",
                (
                    status,
                    json.dumps(usage) if usage else None,
                    error_type,
                    elapsed,
                    ident,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Attempt missing or already finished")

    def summary(self):
        with self.connection() as conn:
            limit = conn.execute(
                "SELECT max_attempts FROM budget WHERE id=1"
            ).fetchone()[0]
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM attempts ORDER BY reserved_at,id"
                )
            ]
            metadata = {
                row["id"]: json.loads(row["settings"])
                for row in conn.execute("SELECT * FROM attempt_metadata")
            }
        for row in rows:
            row["usage"] = json.loads(row["usage"]) if row["usage"] else None
            row["generation_settings"] = metadata.get(row["id"])
        known = [row for row in rows if row["usage"]]
        return {
            "limit": limit,
            "used_slots": len(rows),
            "remaining_slots": limit - len(rows),
            "known_usage_attempts": len(known),
            "unknown_usage_attempts": len(rows) - len(known),
            "known_total_tokens": sum(
                row["usage"].get("total_tokens", 0) or 0 for row in known
            ),
            "cost_amount": None,
            "cost_state": "unknown",
            "semantics": "Reservations are conservative: a process interruption never refunds an uncertain in-flight HTTP attempt.",
            "attempts": rows,
        }


def create_proxy(
    budget, *, upstream_base, provider_key, model, transport=None, trace_dir=None
):
    app = FastAPI(title="D5 durable model budget")
    traces = Path(trace_dir) if trace_dir is not None else None
    if traces is not None:
        traces.mkdir(parents=True, exist_ok=True)
        traces.chmod(0o700)

    @app.get("/health")
    def health():
        return {k: v for k, v in budget.summary().items() if k != "attempts"}

    @app.post("/v1/chat/completions")
    def completion(request: Request, data: dict):
        if data.get("model") != model or data.get("stream") or data.get("n", 1) != 1:
            raise HTTPException(
                400, "Model, streaming or choice count outside the frozen protocol"
            )
        if not isinstance(data.get("messages"), list) or not data["messages"]:
            raise HTTPException(400, "Messages required")
        # Freeze sampling for every baseline/ablation; retain the exact forwarded knobs.
        data["temperature"] = 0
        data["top_p"] = 1
        cap = data.get("max_tokens", data.get("max_completion_tokens", 8192))
        if type(cap) is not int or not 1 <= cap <= 8192:
            raise HTTPException(400, "Output token cap outside frozen protocol")
        if "max_tokens" not in data and "max_completion_tokens" not in data:
            data["max_tokens"] = cap
        body = json.dumps(data, separators=(",", ":")).encode()
        if len(body) > 1_000_000:
            raise HTTPException(413, "Model context request too large")
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        try:
            ident = budget.reserve(token, body)
        except InvalidRunToken as exc:
            raise HTTPException(401, str(exc)) from exc
        except BudgetDenied as exc:
            return JSONResponse(
                {
                    "error": {
                        "message": str(exc),
                        "type": "model_budget_exhausted",
                        "code": "model_budget_exhausted",
                    }
                },
                status_code=429,
            )
        started = time.monotonic()
        own_trace = traces / ident if traces is not None else None

        def trace(name, content):
            if own_trace is None:
                return
            own_trace.mkdir(exist_ok=True)
            own_trace.chmod(0o700)
            path = own_trace / name
            redacted = content.replace(provider_key.encode(), b"[PROVIDER_KEY]")
            if token:
                redacted = redacted.replace(token.encode(), b"[RUN_TOKEN]")
            path.write_bytes(redacted)
            path.chmod(0o600)

        # Evidence for every upstream attempt is outside the diagnostic container.
        # No Authorization headers are recorded. A pre-dispatch I/O error fails closed.
        try:
            trace("request.json", body)
        except OSError:
            budget.finish(ident, error_type="TraceWriteFailedBeforeDispatch")
            raise HTTPException(
                503, "Trace persistence failed before dispatch"
            ) from None
        try:
            # No transport retry; any caller retry must reserve a fresh durable slot.
            with httpx.Client(transport=transport, timeout=180) as client:
                response = client.post(
                    upstream_base.rstrip("/") + "/chat/completions",
                    content=body,
                    headers={
                        "Authorization": "Bearer " + provider_key,
                        "Content-Type": "application/json",
                    },
                )
            try:
                payload = response.json()
                usage = payload.get("usage") if isinstance(payload, dict) else None
                if (
                    not isinstance(usage, dict)
                    or type(usage.get("total_tokens")) is not int
                    or usage["total_tokens"] < 0
                ):
                    usage = None
            except ValueError:
                usage = None
            budget.finish(
                ident,
                status=response.status_code,
                usage=usage,
                elapsed=time.monotonic() - started,
            )
            trace("response.json", response.content)
            trace(
                "outcome.json",
                json.dumps(
                    {"http_status": response.status_code, "dispatched": True}
                ).encode(),
            )
            return Response(
                response.content.replace(provider_key.encode(), b"[REDACTED]"),
                status_code=response.status_code,
                media_type="application/json",
                headers={"X-CloudSRE-Attempt-ID": ident},
            )
        except httpx.HTTPError as exc:
            budget.finish(
                ident, error_type=type(exc).__name__, elapsed=time.monotonic() - started
            )
            trace(
                "outcome.json",
                json.dumps(
                    {
                        "error_type": type(exc).__name__,
                        "dispatch_attempted": True,
                        "usage_unknown": True,
                    }
                ).encode(),
            )
            return JSONResponse(
                {
                    "error": {
                        "message": "Upstream transport failed; attempt retained with unknown usage",
                        "type": "upstream_transport_error",
                    }
                },
                status_code=502,
            )

    return app


def configured_app():
    import os

    budget = ModelBudget(
        os.environ["D5_BUDGET_DATABASE"], int(os.environ.get("D5_BUDGET_LIMIT", "100"))
    )
    return create_proxy(
        budget,
        upstream_base=os.environ["OPENAI_BASE_URL"],
        provider_key=os.environ["DEEPSEEK_API_KEY"],
        model=os.environ["MODEL_NAME"],
        trace_dir=Path(os.environ["D5_BUDGET_DATABASE"]).parent / "traces",
    )
