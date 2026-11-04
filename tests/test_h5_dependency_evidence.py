import json

import httpx
import pytest
from cloudsre.dependency_symptoms import matches_dependency_failure

pytest_plugins = ["test_business"]

CURRENT = "http://inventory:8000/absent-prefix"
EXPECTED = "http://inventory:8000"


def event(**updates):
    return {
        "event": "inventory_rejected",
        "service": "order",
        "status": 404,
        "upstream": CURRENT,
        "request_path": "/reservations",
        **updates,
    }


def test_exact_path_failure_is_supported():
    assert matches_dependency_failure([json.dumps(event())], CURRENT, EXPECTED)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 409},
        {"status": 422},
        {"status": 500},
        {"status": "404"},
        {"service": "gateway"},
        {"upstream": EXPECTED},
        {"upstream": None},
        {"request_path": "/stock"},
        {"event": "unrelated"},
    ],
)
def test_business_errors_and_unbound_logs_rejected(changes):
    assert not matches_dependency_failure(
        [json.dumps(event(**changes))], CURRENT, EXPECTED
    )


@pytest.mark.parametrize(
    "healthy",
    [
        CURRENT,
        "http://other:8000",
        "https://inventory:8000",
        "http://inventory:8000?x=1",
    ],
)
def test_non_path_drift_not_generalized(healthy):
    assert not matches_dependency_failure([json.dumps(event())], CURRENT, healthy)


@pytest.mark.parametrize("line", ["not JSON", "null", "[]", "{}"])
def test_missing_or_malformed_evidence_rejected(line):
    assert not matches_dependency_failure([line], CURRENT, EXPECTED)


@pytest.mark.parametrize("status", [404, 409, 422])
def test_actual_business_rejection_emits_correlated_fields(
    clients, monkeypatch, status
):
    _, order = clients
    records = []
    monkeypatch.setattr(
        "cloudsre.demo.log", lambda name, **kw: records.append({"event": name, **kw})
    )
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(status))
    result = order.post(
        "/orders",
        json={"sku": "test", "quantity": 1},
        headers={"Idempotency-Key": "reject"},
    )
    assert result.status_code == 409
    record = next(x for x in records if x["event"] == "inventory_rejected")
    assert record["upstream"] and record["request_path"] == "/reservations"
    assert record["status"] == status
    # An unchanged configured URL must not turn ordinary rejections into repairs.
    assert not matches_dependency_failure(
        [json.dumps(record)], record["upstream"], record["upstream"]
    )
