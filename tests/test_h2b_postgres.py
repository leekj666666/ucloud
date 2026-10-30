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
