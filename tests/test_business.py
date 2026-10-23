import httpx
import pytest
from cloudsre.demo import DemoBase, Stock, create_demo
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture
def clients(tmp_path):
    url = "sqlite:///" + str(tmp_path / "business.db")
    engine = create_engine(url)
    DemoBase.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Stock(sku="test", available=10))
        s.commit()
    with (
        TestClient(create_demo("inventory", url)) as inventory,
        TestClient(create_demo("order", url)) as order,
    ):
        order.fixture_database_url = url
        yield inventory, order


def test_real_write_and_idempotent_reservation(clients, monkeypatch):
    inventory, order = clients
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: inventory.post(
            "/reservations", json=kwargs["json"], headers=kwargs["headers"]
        ),
    )
    headers = {"Idempotency-Key": "order-one"}
    payload = {"sku": "test", "quantity": 2}
    a = order.post("/orders", json=payload, headers=headers)
    assert a.status_code == 200
    assert a.json()["status"] == "CONFIRMED"
    assert order.get("/orders/" + a.json()["id"]).json() == a.json()
    assert order.post("/orders", json=payload, headers=headers).json() == a.json()
    assert inventory.get("/stock/test").json()["available"] == 8
    assert (
        order.post(
            "/orders", json={**payload, "quantity": 3}, headers=headers
        ).status_code
        == 409
    )
    assert (
        order.post(
            "/orders",
            json={**payload, "quantity": 0},
            headers={"Idempotency-Key": "bad"},
        ).status_code
        == 422
    )


def test_remote_commit_then_timeout_reuses_reservation(clients, monkeypatch):
    inventory, order = clients

    def lost_response(url, **kwargs):
        assert (
            inventory.post(
                "/reservations", json=kwargs["json"], headers=kwargs["headers"]
            ).status_code
            == 200
        )
        raise httpx.ReadTimeout("response lost after commit")

    monkeypatch.setattr(httpx, "post", lost_response)
    payload = {"sku": "test", "quantity": 2}
    headers = {"Idempotency-Key": "timeout-one"}
    assert order.post("/orders", json=payload, headers=headers).status_code == 503
    assert order.get("/health").status_code == 200
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: inventory.post(
            "/reservations", json=kwargs["json"], headers=kwargs["headers"]
        ),
    )
    assert (
        order.post("/orders", json=payload, headers=headers).json()["status"]
        == "CONFIRMED"
    )
    assert inventory.get("/stock/test").json()["available"] == 8


def test_oversell_rejected_and_stock_unchanged(clients):
    inventory, _ = clients
    assert (
        inventory.post(
            "/reservations",
            json={"sku": "test", "quantity": 11},
            headers={"Idempotency-Key": "excess"},
        ).status_code
        == 409
    )
    assert inventory.get("/stock/test").json()["available"] == 10


def test_primary_identity_collision_is_rejected_without_inventory_call(
    clients, monkeypatch
):
    import uuid

    from cloudsre.demo import Order

    inventory, order = clients
    key = "new-key-for-existing-identity"
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "cloudsre-order:" + key))
    # Corrupt/conflicting persisted identity is not a successful idempotent retry.
    engine = create_engine(order.fixture_database_url)
    with Session(engine) as session, session.begin():
        session.add(
            Order(
                id=identity,
                key="different-existing-key",
                sku="test",
                quantity=1,
                status="PENDING",
                trace_id="existing",
                tenant_id="demo",
            )
        )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Inventory must not be called for a conflicting order identity"
        )

    monkeypatch.setattr(httpx, "post", forbidden)
    response = order.post(
        "/orders", json={"sku": "test", "quantity": 2}, headers={"Idempotency-Key": key}
    )
    assert response.status_code == 409
    assert inventory.get("/stock/test").json()["available"] == 10
    engine.dispose()
