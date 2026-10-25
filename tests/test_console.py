import json
import uuid

import pytest
from cloudsre.app import create_app
from cloudsre.domain import Status
from cloudsre.durable import digest
from cloudsre.store import Store
from fastapi.testclient import TestClient

HEADERS = {"X-CloudSRE-Request": "console"}


@pytest.fixture
def console(tmp_path):
    url = "sqlite:///" + str(tmp_path / "console.db")
    root = tmp_path / "state"
    root.mkdir()
    with TestClient(
        create_app(url, console_token="test-access-code", evidence_roots=[root])
    ) as client:
        assert (
            client.post(
                "/console/session", json={"token": "test-access-code"}, headers=HEADERS
            ).status_code
            == 200
        )
        store = Store(url)
        yield client, store, root
        store.engine.dispose()


def create(client):
    data = {
        "title": "订单接口出现错误，调查上游依赖",
        "service": "order",
        "namespace": "sre-demo",
        "request_id": uuid.uuid4().hex,
    }
    response = client.post("/console/incidents", json=data, headers=HEADERS)
    assert response.status_code == 201, response.text
    return data, response.json()


def plan(client, store):
    _, data = create(client)
    ident = data["incident"]["id"]
    run_id = uuid.uuid4().hex
    intent = {
        "id": uuid.uuid4().hex,
        "resource": "sre-demo/Deployment/order",
        "before": {"uid": "specific-uid", "resource_version": "42"},
        "patch": [{"op": "test", "path": "/metadata/resourceVersion", "value": "42"}],
    }
    for old, new in (
        (Status.NEW, Status.INVESTIGATING),
        (Status.INVESTIGATING, Status.PLAN_READY),
        (Status.PLAN_READY, Status.WAITING_APPROVAL),
    ):
        store.transition(ident, old, new)
    store.save_run(
        run_id,
        ident,
        "WAITING_APPROVAL",
        {
            "schema_version": 3,
            "action_intent": intent,
            "events": [],
            "outcome": "awaiting_approval",
        },
    )
    body = {
        "run_id": run_id,
        "action_id": intent["id"],
        "intent_hash": digest(intent),
        "decision": "approve",
        "reason": "已核对目标、证据和版本",
    }
    return ident, run_id, intent, body


def test_console_auth_cookie_and_origin_boundaries(tmp_path):
    with TestClient(
        create_app("sqlite:///" + str(tmp_path / "auth.db"), console_token="access")
    ) as c:
        assert c.get("/console/incidents").status_code == 401
        assert c.post("/console/session", json={"token": "access"}).status_code == 403
        assert (
            c.post(
                "/console/session",
                json={"token": "access"},
                headers={**HEADERS, "Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/console/session", json={"token": "错误访问码"}, headers=HEADERS
            ).status_code
            == 403
        )
        response = c.post("/console/session", json={"token": "access"}, headers=HEADERS)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert (
            c.post(
                "/console/incidents",
                json={},
                headers={"Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        assert c.delete("/console/session", headers=HEADERS).status_code == 200
        assert c.get("/console/session").status_code == 401


def test_create_is_idempotent_and_mode_is_explicit(console):
    c, _, _ = console
    body, first = create(c)
    response = c.post("/console/incidents", json=body, headers=HEADERS)
    assert (
        response.status_code == 200
        and response.json()["incident"]["id"] == first["incident"]["id"]
    )
    assert first["control"]["execution_mode"] == "manual"
    assert (
        c.post(
            "/console/incidents", json={**body, "title": "different"}, headers=HEADERS
        ).status_code
        == 409
    )
    assert (
        c.post(
            "/console/incidents",
            json={**body, "request_id": uuid.uuid4().hex, "execution_mode": "auto"},
            headers=HEADERS,
        ).status_code
        == 422
    )


@pytest.mark.parametrize("field", ["run_id", "action_id", "intent_hash"])
def test_wrong_plan_identity_never_authorizes(console, field):
    c, store, _ = console
    ident, _, _, body = plan(c, store)
    wrong = "0" * (64 if field == "intent_hash" else 32)
    assert (
        c.post(
            f"/console/incidents/{ident}/decisions",
            json={**body, field: wrong},
            headers=HEADERS,
        ).status_code
        == 409
    )
    assert c.get(f"/console/incidents/{ident}").json()["decision"] is None


def test_decision_is_immutable_and_opposite_decision_conflicts(console):
    c, store, _ = console
    ident, _, _, body = plan(c, store)
    first = c.post(f"/console/incidents/{ident}/decisions", json=body, headers=HEADERS)
    assert first.status_code == 200
    repeat = c.post(f"/console/incidents/{ident}/decisions", json=body, headers=HEADERS)
    assert repeat.status_code == 200 and repeat.json()["id"] == first.json()["id"]
    assert (
        c.post(
            f"/console/incidents/{ident}/decisions",
            json={**body, "decision": "reject"},
            headers=HEADERS,
        ).status_code
        == 409
    )
    detail = c.get(f"/console/incidents/{ident}").json()
    assert (
        len([e for e in detail["operator_events"] if e["kind"] == "plan_approve"]) == 1
    )


def test_stop_is_durable_idempotent_and_blocks_approval(console):
    c, store, _ = console
    ident, _, _, body = plan(c, store)
    endpoint = f"/console/incidents/{ident}/stop"
    first = c.post(endpoint, json={"reason": "停止这次演练"}, headers=HEADERS)
    repeat = c.post(endpoint, json={"reason": "不得改写原原因"}, headers=HEADERS)
    assert first.status_code == repeat.status_code == 202
    assert first.json() == repeat.json() and first.json()["acknowledged"] is False
    assert (
        c.post(
            f"/console/incidents/{ident}/decisions", json=body, headers=HEADERS
        ).status_code
        == 409
    )


def test_export_and_evidence_are_incident_bound_and_redacted(console):
    c, store, root = console
    ident, run_id, _, _ = plan(c, store)
    folder = root / run_id / "evidence"
    folder.mkdir(parents=True)
    eid = "e-" + uuid.uuid4().hex
    (folder / (eid + ".json")).write_text(
        json.dumps(
            {
                "id": eid,
                "kind": "logs",
                "data": {
                    "api_key": "private-value",
                    "line": "<script>do_not_execute()</script>",
                },
            }
        )
    )
    data = c.get(f"/console/incidents/{ident}/evidence/{eid}").json()
    assert data["content"]["data"]["api_key"] == "[REDACTED]"
    _, other = create(c)
    assert (
        c.get(
            f"/console/incidents/{other['incident']['id']}/evidence/{eid}"
        ).status_code
        == 404
    )
    assert c.get(f"/console/incidents/{ident}/evidence/not-an-id").status_code == 404
    export = c.get(f"/console/incidents/{ident}/export?format=json")
    assert (
        export.status_code == 200
        and "attachment" in export.headers["content-disposition"]
    )
    assert export.json()["verification_claim"] == "not_verified_recovery"
    assert "private-value" not in export.text
    md = c.get(f"/console/incidents/{ident}/export?format=markdown")
    assert "未知费用不记为零" in md.text


def test_console_scope_and_pagination(console):
    c, _, _ = console
    create(c)
    create(c)
    response = c.get("/console/incidents?scope=console&limit=1")
    assert response.status_code == 200 and response.json()["total"] == 2
    assert len(response.json()["items"]) == 1
    assert c.get("/console/incidents?limit=10000").status_code == 422


def test_formal_report_is_hash_and_incident_bound(console):
    import hashlib

    c, store, root = console
    ident, run_id, intent, _ = plan(c, store)
    path = root / run_id / "incident-report.json"
    path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "incident_id": "another-incident",
            "run_id": run_id,
            "unresolved": ["foreign-report-content"],
        }
    ).encode()
    path.write_bytes(raw)
    payload = {
        "action_intent": intent,
        "report": {"sha256": hashlib.sha256(raw).hexdigest()},
    }
    store.save_run(run_id, ident, "WAITING_APPROVAL", payload)
    response = c.get(f"/console/incidents/{ident}/report")
    assert (
        response.status_code == 200
        and response.json()["report_status"] == "file_unavailable"
    )
    assert "foreign-report-content" not in response.text


def test_alert_namespace_cannot_be_redirected_when_starting(console):
    from cloudsre.domain import IncidentInput

    c, store, _ = console
    row, _ = store.create(
        IncidentInput(
            dedup_key=uuid.uuid4().hex, title="business B alert", service="order"
        )
    )
    store.record_alert(
        row["id"], uuid.uuid4().hex, "firing", {"labels": {"namespace": "sre-demo-b"}}
    )
    endpoint = f"/console/incidents/{row['id']}/start"
    assert (
        c.post(
            endpoint,
            json={"namespace": "sre-demo", "execution_mode": "manual"},
            headers=HEADERS,
        ).status_code
        == 409
    )
    response = c.post(
        endpoint,
        json={"namespace": "sre-demo-b", "execution_mode": "manual"},
        headers=HEADERS,
    )
    assert response.status_code == 200 and response.json()["namespace"] == "sre-demo-b"
