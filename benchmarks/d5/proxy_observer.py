"""Transparent evaluator-only accounting of local proxy budget denials.

Mount this file only into the budget proxy after the frozen matrix stops. It
does not change the frozen proxy, its attempts ledger, or any Agent inputs.
Historical denials cannot be reconstructed and remain unknown.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

LOGGER = logging.getLogger("cloudsre.proxy_denial_observer")
MAX_ERROR_BODY = 65_536


def now():
    return datetime.now(UTC).isoformat()


def readonly(database):
    return sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=30
    )


class DenialStore:
    def __init__(self, database):
        self.path = Path(database).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS observer_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proxy_denials (
                    id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
                    run_id TEXT, request_sha256 TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    http_status INTEGER NOT NULL CHECK(http_status=429),
                    upstream_dispatched INTEGER NOT NULL CHECK(upstream_dispatched=0)
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO observer_metadata VALUES (?,?)",
                ("instrumented_from", now()),
            )
            connection.commit()
        self.path.chmod(0o600)

    def record(self, *, run_id, request_sha256):
        event = {
            "id": uuid.uuid4().hex,
            "occurred_at": now(),
            "run_id": run_id,
            "request_sha256": request_sha256,
            "reason": "model_budget_exhausted",
            "http_status": 429,
            "upstream_dispatched": False,
        }
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.execute(
                "INSERT INTO proxy_denials VALUES (?,?,?,?,?,?,?)",
                (
                    event["id"],
                    event["occurred_at"],
                    run_id,
                    request_sha256,
                    event["reason"],
                    429,
                    0,
                ),
            )
            connection.commit()
        return event


def safe_summary(database):
    """Read a projection without starting instrumentation or touching attempts."""
    database = Path(database)
    coverage = {
        "historical_denials_total": None,
        "historical_denials_state": "not_instrumented",
        "total_denials_including_history": None,
        "request_sha256_semantics": "Exact incoming HTTP body bytes, before the frozen proxy normalizes generation settings",
        "upstream_attempts_are_separate": True,
        "observer_error_log_review_required": True,
        "coverage_note": "Counts include only persisted observed local denials since instrumented_from. Pre-instrumentation totals remain unknown. Review proxy_denial_observer_error stderr events before claiming complete coverage; logging errors never change the original HTTP response.",
    }
    if not database.is_file():
        return {
            **coverage,
            "available": False,
            "instrumented_from": None,
            "observed_local_budget_denials": None,
            "events": [],
        }
    with closing(readonly(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        started = connection.execute(
            "SELECT value FROM observer_metadata WHERE key='instrumented_from'"
        ).fetchone()[0]
        events = [
            dict(row)
            for row in connection.execute(
                "SELECT id,occurred_at,run_id,request_sha256,reason,http_status,upstream_dispatched FROM proxy_denials ORDER BY occurred_at,id"
            )
        ]
    for event in events:
        event["upstream_dispatched"] = bool(event["upstream_dispatched"])
    return {
        **coverage,
        "available": True,
        "instrumented_from": started,
        "observed_local_budget_denials": len(events),
        "events": events,
    }


def run_identity(budget_database, headers):
    """Resolve a token in memory using a read-only query; retain only run ID."""
    header = next(
        (value for key, value in headers if key.lower() == b"authorization"), b""
    )
    if not header.startswith(b"Bearer "):
        return None
    token_hash = hashlib.sha256(header[7:]).hexdigest()
    with closing(readonly(budget_database)) as connection:
        row = connection.execute(
            "SELECT id FROM runs WHERE token_hash=?", (token_hash,)
        ).fetchone()
    return row[0] if row else None


class ProxyDenialObserver:
    def __init__(self, app, *, store, budget_database):
        self.app = app
        self.store = store
        self.budget_database = Path(budget_database)
        self.recording_errors_since_process_start = 0

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/v1/chat/completions"
        ):
            return await self.app(scope, receive, send)
        request_hash = hashlib.sha256()
        request_complete = False
        response_status = None
        upstream_attempt = False
        error_body = bytearray()
        error_body_overflow = False
        terminal_seen = False

        async def observed_receive():
            nonlocal request_complete
            message = await receive()
            if message["type"] == "http.request":
                request_hash.update(message.get("body", b""))
                if not message.get("more_body", False):
                    request_complete = True
            return message

        async def observed_send(message):
            nonlocal response_status, upstream_attempt, error_body_overflow
            nonlocal terminal_seen
            if message["type"] == "http.response.start":
                response_status = message["status"]
                upstream_attempt = any(
                    key.lower() == b"x-cloudsre-attempt-id"
                    for key, _ in message.get("headers", [])
                )
            elif (
                message["type"] == "http.response.body"
                and response_status == 429
                and not upstream_attempt
            ):
                body = message.get("body", b"")
                if len(error_body) + len(body) <= MAX_ERROR_BODY:
                    error_body.extend(body)
                else:
                    error_body_overflow = True
                if not message.get("more_body", False) and not terminal_seen:
                    terminal_seen = True
                    try:
                        payload = (
                            json.loads(error_body) if not error_body_overflow else None
                        )
                    except (ValueError, UnicodeError):
                        payload = None
                    error = payload.get("error") if isinstance(payload, dict) else None
                    if (
                        isinstance(error, dict)
                        and error.get("code") == "model_budget_exhausted"
                    ):
                        try:
                            if not request_complete:
                                raise ValueError("Incoming request body incomplete")

                            def persist_denial():
                                return self.store.record(
                                    run_id=run_identity(
                                        self.budget_database,
                                        scope.get("headers", []),
                                    ),
                                    request_sha256=request_hash.hexdigest(),
                                )

                            # Await durable persistence without blocking other
                            # requests on the ASGI loop. The denied response can
                            # be delayed by observer I/O; timing is not preserved.
                            await asyncio.to_thread(persist_denial)
                        except (OSError, ValueError, sqlite3.Error) as exc:
                            self.recording_errors_since_process_start += 1
                            # Error strings may contain credentials; only type is safe.
                            LOGGER.error(
                                json.dumps(
                                    {
                                        "event": "proxy_denial_observer_error",
                                        "error_type": type(exc).__name__,
                                        "recording_errors_since_process_start": self.recording_errors_since_process_start,
                                        "original_response_preserved": True,
                                    }
                                )
                            )
            # Preserve the original event object, headers, bytes, and frame order.
            await send(message)

        return await self.app(scope, observed_receive, observed_send)


def configured_app():
    from cloudsre.model_budget import configured_app as frozen_proxy

    budget_database = Path(os.environ["D5_BUDGET_DATABASE"]).resolve()
    observer_database = Path(
        os.environ.get(
            "D5_DENIAL_DATABASE", str(budget_database.parent / "proxy-denials.sqlite")
        )
    ).resolve()
    if observer_database == budget_database:
        raise ValueError("Observer and budget databases must be separate")
    return ProxyDenialObserver(
        frozen_proxy(),
        store=DenialStore(observer_database),
        budget_database=budget_database,
    )


def export_summary(database, output):
    result = safe_summary(database)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export"])
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summary = export_summary(args.database, args.output)
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "available",
                    "instrumented_from",
                    "observed_local_budget_denials",
                    "historical_denials_total",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
