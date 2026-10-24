from cloudsre.app import create_app
from cloudsre.store import Store
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


def test_alert_delivery_and_resolution(tmp_path):
    with TestClient(create_app("sqlite:///" + str(tmp_path / "alerts.db"))) as c:
        alert = {
            "status": "firing",
            "labels": {"alertname": "OrderErrors", "service": "order"},
            "startsAt": "2026-10-23T00:00:00Z",
        }
        a = c.post("/webhooks/alertmanager", json={"alerts": [alert]}).json()[
            "incident_ids"
        ][0]
        b = c.post("/webhooks/alertmanager", json={"alerts": [alert]}).json()[
            "incident_ids"
        ][0]
        assert a == b
        c.post(
            "/webhooks/alertmanager", json={"alerts": [{**alert, "status": "resolved"}]}
        )
        assert c.get("/incidents/" + a).json()["status"] == "NEW"
        assert len(c.get("/incidents/" + a + "/alerts").json()) == 2
        newer = {**alert, "startsAt": "2026-10-24T00:00:00Z"}
        assert (
            c.post("/webhooks/alertmanager", json={"alerts": [newer]}).json()[
                "incident_ids"
            ][0]
            != a
        )


def test_migration_adopts_existing_rows(tmp_path):
    url = "sqlite:///" + str(tmp_path / "legacy.db")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE incidents (id VARCHAR(36) PRIMARY KEY, dedup_key VARCHAR(200) UNIQUE NOT NULL, title VARCHAR(300) NOT NULL, service VARCHAR(100) NOT NULL, status VARCHAR(32) NOT NULL, created_at VARCHAR(40) NOT NULL, diagnosis TEXT)"
            )
        )
        c.execute(
            text(
                "INSERT INTO incidents VALUES ('old','old-key','Old incident','order','NEW','2026-10-23',NULL)"
            )
        )
    s = Store(url)
    s.initialize()
    s.initialize()
    assert s.get("old")["title"] == "Old incident"
    with s.engine.connect() as c:
        assert (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "0004"
        )
