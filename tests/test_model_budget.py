import concurrent.futures
import json

import httpx
import pytest
from cloudsre.model_budget import (
    BudgetDenied,
    InvalidRunToken,
    ModelBudget,
    create_proxy,
)
from fastapi.testclient import TestClient


def test_model_budget_concurrent_reservations_cannot_exceed_global_cap(tmp_path):
    budget = ModelBudget(tmp_path / "budget.db", 7)
    tokens = [budget.register(str(index), 8) for index in range(5)]

    def attempt(index):
        try:
            return budget.reserve(tokens[index % 5], b"{}")
        except BudgetDenied:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(attempt, range(50)))
    assert sum(value is not None for value in accepted) == 7
    assert budget.summary()["remaining_slots"] == 0


def test_model_budget_restart_retains_unknown_slots_and_immutable_limit(tmp_path):
    path = tmp_path / "budget.db"
    budget = ModelBudget(path, 2)
    token = budget.register("original", 2)
    budget.reserve(token, b"first transport may be in flight")
    restarted = ModelBudget(path, 2)
    assert restarted.summary()["used_slots"] == 1
    assert restarted.summary()["unknown_usage_attempts"] == 1
    restarted.reserve(token, b"caller retry")
    with pytest.raises(BudgetDenied):
        restarted.reserve(token, b"forbidden third attempt")
    with pytest.raises(ValueError, match="immutable"):
        ModelBudget(path, 3)


def test_model_budget_separates_run_and_batch_limits(tmp_path):
    budget = ModelBudget(tmp_path / "budget.db", 4)
    first = budget.register("first", 1)
    second = budget.register("second", 2)
    budget.reserve(first, b"a")
    with pytest.raises(BudgetDenied):
        budget.reserve(first, b"b")
    budget.reserve(second, b"c")
    assert budget.summary()["used_slots"] == 2
    with pytest.raises(InvalidRunToken):
        budget.reserve("unregistered", b"d")
    assert budget.summary()["used_slots"] == 2


@pytest.mark.parametrize(
    "failure", ["http_error", "transport_error", "no_usage", "success"]
)
def test_proxy_records_each_real_upstream_attempt_and_does_not_forward_secrets(
    tmp_path, failure
):
    observed = []
    key = "provider-private-test-key"

    def upstream(request):
        observed.append(request)
        assert request.headers["authorization"] == "Bearer " + key
        if failure == "transport_error":
            raise httpx.ConnectError("offline", request=request)
        if failure == "http_error":
            return httpx.Response(503, json={"error": {"message": key}})
        if failure == "no_usage":
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(200, json={"choices": [], "usage": {"total_tokens": 12}})

    budget = ModelBudget(tmp_path / "budget.db", 2)
    token = budget.register("run", 2)
    app = create_proxy(
        budget,
        upstream_base="https://provider.test/v1",
        provider_key=key,
        model="fixed",
        transport=httpx.MockTransport(upstream),
        trace_dir=tmp_path / "traces",
    )
    with TestClient(app) as client:
        body = {"model": "fixed", "messages": [{"role": "user", "content": "a"}]}
        for _ in range(2):
            response = client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == {
                "http_error": 503,
                "transport_error": 502,
            }.get(failure, 200)
            assert key not in response.text and token not in response.text
        denied = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer " + token},
        )
        assert denied.status_code == 429
        assert len(observed) == 2
        assert budget.summary()["used_slots"] == 2
        assert budget.summary()["known_usage_attempts"] == (
            2 if failure == "success" else 0
        )
        assert budget.summary()["cost_amount"] is None
        traces = list((tmp_path / "traces").iterdir())
        assert len(traces) == 2
        for trace in traces:
            assert json.loads((trace / "request.json").read_text())["model"] == "fixed"
            assert (trace / "outcome.json").is_file()
            assert (trace / "response.json").exists() is (failure != "transport_error")
            assert key not in "".join(p.read_text() for p in trace.iterdir())
            assert token not in "".join(p.read_text() for p in trace.iterdir())


def test_proxy_invalid_requests_never_spend_provider_budget(tmp_path):
    observed = []

    def upstream(request):
        observed.append(request)
        return httpx.Response(200, json={})

    budget = ModelBudget(tmp_path / "budget.db", 3)
    token = budget.register("run")
    app = create_proxy(
        budget,
        upstream_base="https://provider.test",
        provider_key="private",
        model="fixed",
        transport=httpx.MockTransport(upstream),
    )
    body = {"model": "fixed", "messages": [{"role": "user", "content": "a"}]}
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=body).status_code == 401
        for extra in (
            {"model": "another"},
            {"stream": True},
            {"n": 2},
            {"max_tokens": 8193},
        ):
            assert (
                client.post(
                    "/v1/chat/completions",
                    json={**body, **extra},
                    headers={"Authorization": "Bearer " + token},
                ).status_code
                == 400
            )
    assert not observed and budget.summary()["used_slots"] == 0
    assert "token_hash" not in json.dumps(budget.summary())
