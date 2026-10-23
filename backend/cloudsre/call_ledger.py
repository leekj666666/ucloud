"""Durable per-HTTP-attempt accounting. Never stores keys or prompt contents."""

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


class CallLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def append(self, item):
        item = {**item, "at": datetime.now(UTC).isoformat()}
        with self.lock, self.path.open("a") as stream:
            stream.write(json.dumps(item) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def begin(self, request):
        ident = uuid.uuid4().hex
        body = request.content
        try:
            model = json.loads(body).get("model")
        except (ValueError, TypeError):
            model = None
        self.append(
            {
                "id": ident,
                "event": "started",
                "model": model,
                "endpoint": str(request.url).split("?")[0],
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "usage_state": "unknown",
                "cost_state": "unknown",
            }
        )
        return ident

    def finish(self, ident, started, response=None, error=None):
        record = {
            "id": ident,
            "event": "finished",
            "elapsed_seconds": time.monotonic() - started,
            "cost_state": "unknown",
            "cost_amount": None,
            "price_version": None,
        }
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                body = {}
            usage = body.get("usage") if isinstance(body, dict) else None
            record.update(
                {
                    "http_status": response.status_code,
                    "provider_request_id": body.get("id")
                    if isinstance(body, dict)
                    else None,
                    "usage": usage,
                    "usage_state": "known" if usage else "unknown",
                }
            )
        else:
            record.update(
                {"error_type": type(error).__name__, "usage_state": "unknown"}
            )
        self.append(record)


def install_http_ledger(path, base_url):
    ledger = CallLedger(path)
    host = urlparse(base_url).hostname
    sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

    def matches(request):
        return (
            request.method == "POST"
            and request.url.host == host
            and request.url.path.endswith("/chat/completions")
        )

    def send(client, request, *args, **kwargs):
        if not matches(request):
            return sync_send(client, request, *args, **kwargs)
        ident, started = ledger.begin(request), time.monotonic()
        try:
            response = sync_send(client, request, *args, **kwargs)
            response.read()
        except BaseException as exc:
            ledger.finish(ident, started, error=exc)
            raise
        ledger.finish(ident, started, response=response)
        return response

    async def asend(client, request, *args, **kwargs):
        if not matches(request):
            return await async_send(client, request, *args, **kwargs)
        ident, started = ledger.begin(request), time.monotonic()
        try:
            response = await async_send(client, request, *args, **kwargs)
            await response.aread()
        except BaseException as exc:
            ledger.finish(ident, started, error=exc)
            raise
        ledger.finish(ident, started, response=response)
        return response

    httpx.Client.send, httpx.AsyncClient.send = send, asend
    return ledger


def summarize(path):
    path = Path(path)
    entries, truncated = [], False
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                truncated = True
    calls = {}
    for entry in entries:
        calls.setdefault(entry["id"], {}).update(entry)
    known = [r for r in calls.values() if r.get("usage_state") == "known"]
    totals = {
        key: sum((r.get("usage") or {}).get(key, 0) or 0 for r in known)
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
    }
    # Cached splits missing in a provider response remain explicitly unknown.
    cache_known = bool(known) and all(
        "prompt_cache_hit_tokens" in r["usage"]
        and "prompt_cache_miss_tokens" in r["usage"]
        for r in known
    )
    if not cache_known:
        totals["prompt_cache_hit_tokens"] = None
        totals["prompt_cache_miss_tokens"] = None
    return {
        "http_attempts": len(calls),
        "known_usage_calls": len(known),
        "unknown_usage_calls": len(calls) - len(known),
        "incomplete_ledger_line": truncated,
        "known_token_subtotal": totals,
        "cached_split_known": cache_known,
        "cost_state": "unknown",
        "cost_amount": None,
        "currency": None,
        "price_version": None,
        "calls": list(calls.values()),
        "ledger_path": str(path),
    }
