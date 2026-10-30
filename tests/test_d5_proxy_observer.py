import asyncio
import concurrent.futures
import hashlib
import json
import sqlite3
import threading

import httpx
import pytest
from cloudsre.model_budget import ModelBudget, create_proxy
from fastapi.testclient import TestClient

from benchmarks.d5.proxy_observer import (
    DenialStore,
    ProxyDenialObserver,
    configured_app,
    export_summary,
    safe_summary,
)


def setup_proxy(tmp_path, *, cap=7, upstream_status=200, upstream_code=None):
    attempts = []
    budget_path = tmp_path / "batch.sqlite"
    budget = ModelBudget(budget_path, cap)
    token = budget.register("known-run", 8)
    observer_path = tmp_path / "observer.sqlite"

    def upstream(request):
        attempts.append(request)
        if upstream_status != 200:
            return httpx.Response(
                upstream_status, json={"error": {"code": upstream_code}}
            )
        return httpx.Response(200, json={"choices": [], "usage": {"total_tokens": 12}})

    original = create_proxy(
        budget,
        upstream_base="https://provider.test/v1",
        provider_key="private-provider-key",
        model="fixed",
        transport=httpx.MockTransport(upstream),
    )
    observer = ProxyDenialObserver(
        original, store=DenialStore(observer_path), budget_database=budget_path
    )
    return budget, token, observer_path, attempts, original, observer


def test_concurrent_denials_are_durable_separate_and_do_not_add_upstream_calls(
    tmp_path,
):
    budget, token, observer_path, attempts, _, observer = setup_proxy(tmp_path)
    body = b'{"model":"fixed","messages":[{"role":"user","content":"same"}]}'
    with TestClient(observer) as client:

        def dispatch(_):
            return client.post(
                "/v1/chat/completions",
                content=body,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(dispatch, range(24)))
    assert statuses.count(200) == len(attempts) == budget.summary()["used_slots"] == 7
    assert statuses.count(429) == 17
    summary = safe_summary(observer_path)
    assert summary["observed_local_budget_denials"] == 17
    assert summary["historical_denials_total"] is None
    assert summary["total_denials_including_history"] is None
    assert all(
        event["run_id"] == "known-run"
        and event["request_sha256"] == hashlib.sha256(body).hexdigest()
        and event["upstream_dispatched"] is False
        and event["reason"] == "model_budget_exhausted"
        for event in summary["events"]
    )
    serialized = json.dumps(summary)
    assert token not in serialized and "private-provider-key" not in serialized
    assert hashlib.sha256(token.encode()).hexdigest() not in serialized
    assert "token_hash" not in serialized
    assert observer.recording_errors_since_process_start == 0


def test_restart_preserves_first_instrumentation_and_separate_counts(tmp_path):
    budget, token, observer_path, attempts, original, observer = setup_proxy(
        tmp_path, cap=1
    )
    body = {"model": "fixed", "messages": [{"role": "user", "content": "a"}]}
    headers = {"Authorization": "Bearer " + token}
    first = safe_summary(observer_path)["instrumented_from"]
    with TestClient(observer) as client:
        assert (
            client.post("/v1/chat/completions", json=body, headers=headers).status_code
            == 200
        )
        assert (
            client.post("/v1/chat/completions", json=body, headers=headers).status_code
            == 429
        )
    restarted = ProxyDenialObserver(
        original,
        store=DenialStore(observer_path),
        budget_database=budget.path,
    )
    with TestClient(restarted) as client:
        assert (
            client.post("/v1/chat/completions", json=body, headers=headers).status_code
            == 429
        )
    summary = safe_summary(observer_path)
    assert summary["instrumented_from"] == first
    assert summary["observed_local_budget_denials"] == 2
    assert len(attempts) == budget.summary()["used_slots"] == 1
    with sqlite3.connect(budget.path) as connection:
        tables = {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "proxy_denials" not in tables and "observer_metadata" not in tables


def test_upstream_429_even_with_same_error_code_is_not_a_local_denial(tmp_path):
    budget, token, observer_path, attempts, _, observer = setup_proxy(
        tmp_path, cap=1, upstream_status=429, upstream_code="model_budget_exhausted"
    )
    with TestClient(observer) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "fixed", "messages": [{"role": "user", "content": "a"}]},
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 429 and response.headers["x-cloudsre-attempt-id"]
    assert len(attempts) == budget.summary()["used_slots"] == 1
    assert safe_summary(observer_path)["observed_local_budget_denials"] == 0


def test_invalid_requests_are_unchanged_and_do_not_look_like_budget_denials(
    tmp_path,
):
    budget, token, observer_path, attempts, _, observer = setup_proxy(tmp_path)
    body = {"model": "fixed", "messages": [{"role": "user", "content": "a"}]}
    with TestClient(observer) as client:
        assert client.post("/v1/chat/completions", json=body).status_code == 401
        assert (
            client.post(
                "/v1/chat/completions",
                json={**body, "model": "other"},
                headers={"Authorization": "Bearer " + token},
            ).status_code
            == 400
        )
        assert client.get("/health").json()["used_slots"] == 0
    assert not attempts and budget.summary()["used_slots"] == 0
    assert safe_summary(observer_path)["observed_local_budget_denials"] == 0


def test_streamed_asgi_frames_headers_and_body_are_passed_by_identity(
    tmp_path, monkeypatch
):
    budget, token, observer_path, _, _, _ = setup_proxy(tmp_path)
    incoming = [
        {"type": "http.request", "body": b'{"part":', "more_body": True},
        {"type": "http.request", "body": b'"exact"}', "more_body": False},
    ]
    outgoing = [
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [(b"x-exact", b"value")],
        },
        {"type": "http.response.body", "body": b'{"error":{"code":', "more_body": True},
        {
            "type": "http.response.body",
            "body": b'"model_budget_exhausted"}}',
            "more_body": False,
        },
    ]
    received, sent = [], []
    loop_thread = threading.get_ident()

    async def app(scope, receive, send):
        received.extend([await receive(), await receive()])
        for message in outgoing:
            await send(message)

    async def invoke():
        events = iter(incoming)

        async def receive():
            return next(events)

        async def send(message):
            sent.append(message)

        store = DenialStore(observer_path)
        record = store.record

        def record_off_loop(**kwargs):
            assert threading.get_ident() != loop_thread
            return record(**kwargs)

        monkeypatch.setattr(store, "record", record_off_loop)
        observer = ProxyDenialObserver(app, store=store, budget_database=budget.path)
        await observer(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [(b"authorization", b"Bearer " + token.encode())],
            },
            receive,
            send,
        )

    asyncio.run(invoke())
    assert all(a is b for a, b in zip(received, incoming, strict=True))
    assert all(a is b for a, b in zip(sent, outgoing, strict=True))
    summary = safe_summary(observer_path)
    assert summary["observed_local_budget_denials"] == 1
    assert (
        summary["events"][0]["request_sha256"]
        == hashlib.sha256(b'{"part":"exact"}').hexdigest()
    )
    assert budget.summary()["used_slots"] == 0


def test_observer_failure_preserves_response_and_does_not_log_secret_error_text(
    tmp_path, monkeypatch, caplog
):
    budget, token, observer_path, attempts, _, observer = setup_proxy(tmp_path, cap=1)
    body = {"model": "fixed", "messages": [{"role": "user", "content": "a"}]}

    def fail(**kwargs):
        raise sqlite3.OperationalError("secret-error-detail")

    monkeypatch.setattr(observer.store, "record", fail)
    with TestClient(observer) as client:
        for status in (200, 429):
            response = client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == status
        assert response.json()["error"]["code"] == "model_budget_exhausted"
    assert len(attempts) == budget.summary()["used_slots"] == 1
    assert observer.recording_errors_since_process_start == 1
    assert "proxy_denial_observer_error" in caplog.text
    assert "secret-error-detail" not in caplog.text and token not in caplog.text
    assert safe_summary(observer_path)["observer_error_log_review_required"] is True


def test_export_is_readonly_and_does_not_backfill_unknown_history(tmp_path):
    database = tmp_path / "observer.sqlite"
    output = tmp_path / "safe-summary.json"
    missing = export_summary(database, output)
    assert missing["available"] is False and not database.exists()
    assert missing["historical_denials_total"] is None
    store = DenialStore(database)
    store.record(run_id="run", request_sha256="a" * 64)
    before = database.read_bytes()
    summary = export_summary(database, output)
    assert database.read_bytes() == before
    assert json.loads(output.read_text()) == summary
    assert summary["observed_local_budget_denials"] == 1
    assert summary["historical_denials_total"] is None
    assert summary["total_denials_including_history"] is None


def test_factory_refuses_observer_aliasing_budget_before_touching_database(
    tmp_path, monkeypatch
):
    database = tmp_path / "budget.sqlite"
    monkeypatch.setenv("D5_BUDGET_DATABASE", str(database))
    monkeypatch.setenv("D5_DENIAL_DATABASE", str(tmp_path / "unused/../budget.sqlite"))
    with pytest.raises(ValueError, match="must be separate"):
        configured_app()
    assert not database.exists()
