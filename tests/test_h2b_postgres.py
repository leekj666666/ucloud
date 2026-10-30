import os
import uuid

import pytest
from cloudsre.domain import IncidentInput

pytest_plugins = ["test_durable_postgres"]

pytestmark = pytest.mark.skipif(
    not os.environ.get("SRE_TEST_DATABASE_URL"), reason="isolated PostgreSQL required"
)


def test_reinvestigation_checkpoint_survives_lease_reacquisition(durable):
    ident = durable.store.create(
        IncidentInput(dedup_key=uuid.uuid4().hex, title="h2b", service="order")
    )[0]["id"]
    run = uuid.uuid4().hex
    payload = {"incident_id": ident, "run_id": run, "events": [], "h2b_rounds": 0}
    with durable.acquire(ident, ident) as lease:
        for before, after in [
            ("NEW", "INVESTIGATING"),
            ("INVESTIGATING", "PLAN_READY"),
            ("PLAN_READY", "WAITING_APPROVAL"),
            ("WAITING_APPROVAL", "EXECUTING"),
            ("EXECUTING", "VERIFYING"),
        ]:
            lease.checkpoint(run, before, after, payload)
        payload.update(
            h2b_rounds=1,
            h2b_feedback={"failed_checks": ["all_invariants_succeeded"]},
            diagnoses=[{"id": uuid.uuid4().hex, "state": "prepared"}],
        )
        lease.checkpoint(run, "VERIFYING", "INVESTIGATING", payload)
        first_epoch = lease.epoch
    with durable.acquire(ident, ident) as lease:
        restored = durable.store.runs(ident)[-1]
        assert lease.epoch > first_epoch
        assert restored["phase"] == "INVESTIGATING"
        assert restored["payload"]["h2b_rounds"] == 1
        assert restored["payload"]["diagnoses"][0]["state"] == "prepared"
        lease.checkpoint(run, "INVESTIGATING", "PLAN_READY", restored["payload"])
        lease.checkpoint(run, "PLAN_READY", "WAITING_APPROVAL", restored["payload"])
        assert durable.store.get(ident)["status"] == "WAITING_APPROVAL"


def test_h2b_killed_process_retains_feedback_and_new_diagnosis(durable, tmp_path):
    import json
    import signal
    import subprocess
    import sys
    import time

    ident = durable.store.create(
        IncidentInput(dedup_key=uuid.uuid4().hex, title="h2b crash", service="order")
    )[0]["id"]
    ready = tmp_path / "ready.json"
    script = """
import os,sys,json,time
from pathlib import Path
from cloudsre.store import Store
from cloudsre.durable import DurableStore
d=DurableStore(Store(os.environ['SRE_TEST_DATABASE_URL']))
ident=sys.argv[1]
p={'incident_id':ident,'run_id':ident,'events':[]}
with d.acquire(ident,ident) as lease:
 for a,b in [('NEW','INVESTIGATING'),('INVESTIGATING','PLAN_READY'),('PLAN_READY','EXECUTING'),('EXECUTING','VERIFYING')]:
  lease.checkpoint(ident,a,b,p)
 p.update(h2b_rounds=1,h2b_feedback={'failed_checks':['create_order_http_5xx']},diagnoses=[{'id':'same-prepared-job','state':'prepared'}])
 lease.checkpoint(ident,'VERIFYING','INVESTIGATING',p)
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
        with durable.acquire(ident, ident) as lease:
            assert lease.epoch == json.loads(ready.read_text())["epoch"] + 1
            restored = durable.store.runs(ident)[-1]
            assert restored["phase"] == "INVESTIGATING"
            assert restored["payload"]["h2b_rounds"] == 1
            assert restored["payload"]["diagnoses"] == [
                {"id": "same-prepared-job", "state": "prepared"}
            ]
            lease.checkpoint(ident, "INVESTIGATING", "PLAN_READY", restored["payload"])
        assert len(durable.store.runs(ident)) == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
