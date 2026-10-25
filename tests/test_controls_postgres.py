"""Actual PostgreSQL serialization of operator authority and external dispatch."""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cloudsre.controls import ControlConflict, Controls, OperatorEvent, OperatorStop
from cloudsre.domain import IncidentInput, Status
from cloudsre.durable import digest
from cloudsre.store import Store
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

URL = os.environ.get("SRE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL required")


@pytest.fixture(scope="module")
def controls():
    admin = create_engine(URL)
    schema = "d4_" + uuid.uuid4().hex
    with admin.begin() as c:
        c.execute(text('CREATE SCHEMA "' + schema + '"'))
    store = Store(
        make_url(URL).update_query_dict({"options": "-csearch_path=" + schema})
    )
    store.initialize()
    try:
        yield Controls(store)
    finally:
        store.engine.dispose()
        with admin.begin() as c:
            c.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def task(controls):
    return controls.create(
        title="approval integration",
        service="order",
        namespace="sre-demo",
        execution_mode="manual",
        request_id=uuid.uuid4().hex,
        actor="test-operator",
    )[0]["id"]


def ready(controls):
    ident = task(controls)
    run = uuid.uuid4().hex
    intent = {
        "id": uuid.uuid4().hex,
        "resource": "sre-demo/Deployment/order",
        "value": "bounded",
    }
    for old, new in (
        (Status.NEW, Status.INVESTIGATING),
        (Status.INVESTIGATING, Status.PLAN_READY),
        (Status.PLAN_READY, Status.WAITING_APPROVAL),
    ):
        controls.store.transition(ident, old, new)
    controls.store.save_run(run, ident, "WAITING_APPROVAL", {"action_intent": intent})
    return ident, run, intent


def approve(controls, ident, run, intent):
    return controls.decide(
        ident,
        run_id=run,
        action_id=intent["id"],
        intent_hash=digest(intent),
        decision="approve",
        reason="reviewed",
        actor="test-operator",
    )


def test_concurrent_create_has_one_control_and_one_audit(controls):
    request_id = uuid.uuid4().hex
    barrier = threading.Barrier(4)

    def create(_):
        barrier.wait(timeout=5)
        return controls.create(
            title="same request",
            service="order",
            namespace="sre-demo",
            execution_mode="manual",
            request_id=request_id,
            actor="test-operator",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert len({row[0]["id"] for row in results}) == 1
    assert sum(created for _, created in results) == 1
    ident = results[0][0]["id"]
    with controls.store.sessions() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(OperatorEvent)
                .where(OperatorEvent.incident_id == ident)
            )
            == 1
        )


def test_opposite_decisions_race_has_one_immutable_winner(controls):
    ident, run, intent = ready(controls)
    barrier = threading.Barrier(2)

    def decide(decision):
        barrier.wait(timeout=5)
        try:
            return controls.decide(
                ident,
                run_id=run,
                action_id=intent["id"],
                intent_hash=digest(intent),
                decision=decision,
                reason="racing operator decision",
                actor="test-operator",
            )["decision"]
        except ControlConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ["approve", "reject"]))
    assert results.count("conflict") == 1
    assert controls.decision(intent["id"])["decision"] in {"approve", "reject"}


def test_stop_accepted_before_dispatch_prevents_write(controls):
    ident, run, intent = ready(controls)
    approve(controls, ident, run, intent)
    controls.stop(ident, "stop before network dispatch", "test-operator")
    writes = []
    with pytest.raises(OperatorStop), controls.dispatch_guard(ident, intent, run):
        writes.append("must never dispatch")
    assert writes == []


def test_stop_waits_for_inflight_dispatch_and_fences_following_write(controls):
    ident, run, intent = ready(controls)
    approve(controls, ident, run, intent)
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    order = []

    def dispatch():
        with controls.dispatch_guard(ident, intent, run):
            entered.set()
            assert release.wait(timeout=5)
            order.append("inflight_returned")

    def stop():
        controls.stop(ident, "stop while request already in flight", "test-operator")
        order.append("stop_accepted")
        stopped.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(dispatch)
        assert entered.wait(timeout=5)
        stopper = pool.submit(stop)
        time.sleep(0.15)
        assert not stopped.is_set()
        release.set()
        writer.result(timeout=5)
        stopper.result(timeout=5)
    assert order == ["inflight_returned", "stop_accepted"]
    with pytest.raises(OperatorStop), controls.dispatch_guard(ident, intent, run):
        pytest.fail("New dispatch after accepted stop")


def test_pending_control_task_is_not_starved_by_uncontrolled_alerts(controls):
    ident = task(controls)
    for i in range(105):
        controls.store.create(
            IncidentInput(
                dedup_key=uuid.uuid4().hex,
                title="uncontrolled legacy alert " + str(i),
                service="order",
            )
        )
    assert ident not in {row["id"] for row in controls.store.list()}
    assert ident in {row["id"] for row in controls.pending()}
