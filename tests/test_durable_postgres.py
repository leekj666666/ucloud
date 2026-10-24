"""Real PostgreSQL session loss, process death, fencing and transaction checks."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cloudsre.domain import IncidentInput
from cloudsre.durable import DurableStore, LeaseLost
from cloudsre.store import Store
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, PendingRollbackError

URL = os.environ.get("SRE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL required")


@pytest.fixture(scope="module")
def durable():
    admin = create_engine(URL)
    schema = "d3_" + uuid.uuid4().hex
    with admin.begin() as c:
        c.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = make_url(URL).update_query_dict({"options": "-csearch_path=" + schema})
    store = Store(isolated)
    store.initialize()
    try:
        yield DurableStore(store)
    finally:
        store.engine.dispose()
        with admin.begin() as c:
            c.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def incident(durable):
    return durable.store.create(
        IncidentInput(dedup_key=uuid.uuid4().hex, title="lease test", service="order")
    )[0]["id"]


def payload(ident, run="run"):
    return {"incident_id": ident, "run_id": run, "outcome": "running", "events": []}


def test_same_resource_queues_and_independent_resources_overlap(durable):
    first, second, third = [incident(durable) for _ in range(3)]
    resource = uuid.uuid4().hex
    with durable.acquire(first, resource) as a:
        assert a is not None
        with durable.acquire(second, resource) as blocked:
            assert blocked is None
        with durable.acquire(first, resource + "other") as duplicate:
            assert duplicate is None
        with durable.acquire(third, resource + "other") as independent:
            assert independent is not None
    with durable.acquire(second, resource) as next_owner:
        assert next_owner is not None


def test_two_event_checkpoints_run_concurrently(durable):
    ids = [incident(durable), incident(durable)]
    barrier = Barrier(2)

    def run(ident):
        with durable.acquire(ident, ident) as lease:
            barrier.wait(timeout=5)
            lease.checkpoint(ident, "NEW", "INVESTIGATING", payload(ident, ident))
            barrier.wait(timeout=5)
            return durable.store.get(ident)["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ids)) == ["INVESTIGATING"] * 2


def test_intent_conflict_rolls_back_state_journal_and_audit(durable):
    ident = incident(durable)
    run = uuid.uuid4().hex
    intent = {"id": uuid.uuid4().hex, "expected_version": "12", "new": "safe"}
    with durable.acquire(ident, ident) as lease:
        lease.checkpoint(
            run, "NEW", "INVESTIGATING", payload(ident, run), intent=intent
        )
        before = durable.store.runs(ident)
        with pytest.raises(ValueError, match="Immutable"):
            lease.checkpoint(
                run,
                "INVESTIGATING",
                "PLAN_READY",
                {**payload(ident, run), "new_field": True},
                intent={**intent, "new": "different"},
            )
        assert durable.store.get(ident)["status"] == "INVESTIGATING"
        assert durable.store.runs(ident) == before
        with durable.store.engine.connect() as c:
            audits = list(
                c.execute(
                    select(durable.tables["workflow_audit"]).where(
                        durable.tables["workflow_audit"].c.incident_id == ident
                    )
                )
            )
        assert len(audits) == 1


def test_released_owner_cannot_commit_and_epoch_increases(durable):
    ident = incident(durable)
    run = uuid.uuid4().hex
    with durable.acquire(ident, ident) as old:
        old.checkpoint(run, "NEW", "INVESTIGATING", payload(ident, run))
        epoch = old.epoch
    with durable.acquire(ident, ident) as new:
        assert new.epoch == epoch + 1
        with pytest.raises(LeaseLost):
            old.check()
        new.checkpoint(run, "INVESTIGATING", "PLAN_READY", payload(ident, run))


def test_killed_worker_releases_lease_and_retains_atomic_intent(durable, tmp_path):
    ident = incident(durable)
    ready = tmp_path / "ready.json"
    script = """
import json,os,sys,time
from pathlib import Path
from cloudsre.store import Store
from cloudsre.durable import DurableStore
d=DurableStore(Store(os.environ['SRE_TEST_DATABASE_URL']))
ident=sys.argv[1]
with d.acquire(ident,ident) as lease:
 lease.checkpoint(ident,'NEW','INVESTIGATING',{'incident_id':ident,'run_id':ident,'outcome':'running'},intent={'id':ident,'before':'v1','after':'v2'})
 Path(sys.argv[2]).write_text(json.dumps({'epoch':lease.epoch}))
 while True: time.sleep(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, ident, str(ready)],
        env={
            **os.environ,
            "SRE_TEST_DATABASE_URL": durable.store.engine.url.render_as_string(
                hide_password=False
            ),
        },
    )
    try:
        end = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        assert ready.exists()
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        with durable.acquire(ident, ident) as resumed:
            assert resumed is not None
            assert resumed.epoch == json.loads(ready.read_text())["epoch"] + 1
            assert resumed.action(ident)["spec"]["after"] == "v2"
            resumed.checkpoint(
                ident, "INVESTIGATING", "PLAN_READY", payload(ident, ident)
            )
        assert len(durable.store.runs(ident)) == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_server_session_termination_fences_old_connection(durable):
    ident = incident(durable)
    with (
        pytest.raises((OperationalError, LeaseLost, PendingRollbackError)) as failure,
        durable.acquire(ident, ident) as lease,
    ):
        with durable.store.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as admin:
            assert admin.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": lease.pid}
            ).scalar()
        lease.check()
    assert type(failure.value).__name__ in {
        "OperationalError",
        "LeaseLost",
        "PendingRollbackError",
    }
    with durable.acquire(ident, ident) as new:
        assert new is not None
        new.check()
