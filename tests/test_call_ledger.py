import json
import time

import httpx
from cloudsre.call_ledger import CallLedger, install_http_ledger, summarize


def test_started_but_killed_call_is_unknown_not_free(tmp_path):
    path = tmp_path / "calls.jsonl"
    ledger = CallLedger(path)
    ledger.begin(
        httpx.Request(
            "POST",
            "https://api.example.test/chat/completions",
            json={"model": "test", "messages": [{"content": "sensitive prompt"}]},
            headers={"Authorization": "Bearer example-secret"},
        )
    )
    result = summarize(path)
    assert result["unknown_usage_calls"] == 1
    assert result["cost_amount"] is None
    assert (
        "example-secret" not in path.read_text()
        and "sensitive prompt" not in path.read_text()
    )


def test_usage_cache_fields_and_failed_attempt_are_retained(tmp_path):
    path = tmp_path / "calls.jsonl"
    ledger = CallLedger(path)
    req = httpx.Request(
        "POST", "https://api.example.test/chat/completions", json={"model": "test"}
    )
    ident = ledger.begin(req)
    ledger.finish(
        ident,
        time.monotonic(),
        httpx.Response(
            200,
            json={
                "id": "provider-id",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "prompt_cache_hit_tokens": 6,
                    "prompt_cache_miss_tokens": 4,
                },
            },
        ),
    )
    ident = ledger.begin(req)
    ledger.finish(ident, time.monotonic(), error=httpx.ReadTimeout("test"))
    result = summarize(path)
    assert result["http_attempts"] == 2 and result["unknown_usage_calls"] == 1
    assert result["known_token_subtotal"]["total_tokens"] == 12
    assert result["cached_split_known"] and result["cost_amount"] is None
    assert len(path.read_text().splitlines()) == 4


def test_http_transport_hook_records_each_actual_attempt(tmp_path, monkeypatch):
    original, async_original = httpx.Client.send, httpx.AsyncClient.send
    path = tmp_path / "calls.jsonl"
    try:
        install_http_ledger(path, "https://api.example.test")
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, json={"id": "r", "usage": {"total_tokens": 4}}
                )
            )
        ) as client:
            for _ in range(2):
                client.post(
                    "https://api.example.test/chat/completions", json={"model": "test"}
                )
        assert summarize(path)["http_attempts"] == 2
        assert all(
            "Authorization" not in json.dumps(x) for x in summarize(path)["calls"]
        )
    finally:
        monkeypatch.setattr(httpx.Client, "send", original)
        monkeypatch.setattr(httpx.AsyncClient, "send", async_original)
