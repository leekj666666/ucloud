import json

import httpx
import pytest
from cloudsre.business_verify import verify
from cloudsre.demo import DemoBase, Stock, Tenant
from cloudsre.verification_data import VerificationData
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session


@pytest.fixture
def database(tmp_path):
    url = "sqlite:///" + str(tmp_path / "verify.db")
    engine = create_engine(url)
    DemoBase.metadata.create_all(engine)
    yield url, engine
    engine.dispose()


@pytest.mark.parametrize(
    "mode", ["correct", "conflict_side_effect", "wrong_payload", "unavailable"]
)
def test_verifier_rejects_false_success_and_cleans_owned_data(
    database, tmp_path, monkeypatch, mode
):
    url, engine = database
    available = 100
    record = {}

    def transport(request):
        nonlocal available, record
        if "/stock/" in str(request.url):
            return httpx.Response(200, json={"available": available})
        if request.method == "GET":
            return httpx.Response(200, json=record)
        payload = json.loads(request.content)
        if mode == "unavailable":
            return httpx.Response(503)
        if payload["quantity"] == 3:
            if mode == "conflict_side_effect":
                available -= 1
            return httpx.Response(409)
        if not record:
            available -= 2
            record = {"id": "id", "status": "CONFIRMED", **payload}
            if mode == "wrong_payload":
                record["sku"] = "another-sku"
        return httpx.Response(200, json=record, headers={"x-trace-id": "a" * 32})

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(transport), **kw),
    )
    result = verify(
        "http://gateway",
        "http://inventory",
        database_url=url,
        evidence_dir=tmp_path / "evidence",
    )
    assert result["passed"] == (mode == "correct")
    assert result["cleanup"]["cleaned"]
    assert list((tmp_path / "evidence").glob("verify-*.json"))
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Tenant)) == 0
        assert s.scalar(select(func.count()).select_from(Stock)) == 0


def test_cleanup_refuses_business_tenant_and_is_idempotent(database, tmp_path):
    url, engine = database
    fixture = VerificationData(url, tmp_path)
    fixture.create()
    with Session(engine) as s, s.begin():
        s.get(Tenant, fixture.tenant).purpose = "business"
    with pytest.raises(ValueError):
        fixture.cleanup()
    with Session(engine) as s, s.begin():
        assert s.get(Stock, fixture.sku).available == 100
        s.get(Tenant, fixture.tenant).purpose = "verification"
    assert fixture.cleanup()["cleaned"]
    assert fixture.cleanup()["already_absent"]
    fixture.close()
