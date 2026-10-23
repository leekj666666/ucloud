from concurrent.futures import ThreadPoolExecutor

import pytest
from cloudsre.app import create_app
from cloudsre.domain import IncidentInput, Status
from cloudsre.store import Store
from fastapi.testclient import TestClient


def test_api_persistence_and_dedup(tmp_path):
    url = "sqlite:///" + str(tmp_path / "incidents.db")
    payload = {"dedup_key": "alert-1", "title": "Order failures", "service": "order"}
    with TestClient(create_app(url)) as client:
        first = client.post("/incidents", json=payload)
        assert first.status_code == 201
        second = client.post("/incidents", json=payload)
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert (
            client.post(
                "/incidents", json={**payload, "service": "inventory"}
            ).status_code
            == 409
        )
        ident = first.json()["id"]
    with TestClient(create_app(url)) as client:
        assert client.get("/incidents/" + ident).json()["title"] == "Order failures"
        assert client.get("/incidents/missing").status_code == 404


def test_only_one_worker_can_claim(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "claim.db"))
    s.initialize()
    row, _ = s.create(IncidentInput(dedup_key="one", title="test", service="order"))

    def claim(_):
        try:
            s.transition(row["id"], Status.NEW, Status.INVESTIGATING)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, range(2))) == 1
    with pytest.raises(ValueError):
        s.transition(row["id"], Status.INVESTIGATING, Status.NEW)
