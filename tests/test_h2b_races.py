import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import test_controls_postgres as helpers
from cloudsre.controls import ControlConflict, OperatorStop
from cloudsre.durable import digest

pytest_plugins = ["test_controls_postgres"]
pytestmark = helpers.pytestmark


@pytest.mark.parametrize("repeat", range(10))
def test_second_round_stop_approval_race_never_allows_later_dispatch(controls, repeat):
    ident, run, old = helpers.ready(controls)
    helpers.approve(controls, ident, run, old)
    for before, after in [
        ("WAITING_APPROVAL", "EXECUTING"),
        ("EXECUTING", "VERIFYING"),
        ("VERIFYING", "INVESTIGATING"),
        ("INVESTIGATING", "PLAN_READY"),
        ("PLAN_READY", "WAITING_APPROVAL"),
    ]:
        controls.store.transition(ident, before, after)
    fresh = {**old, "id": uuid.uuid4().hex, "value": "second-round"}
    controls.store.save_run(
        run, ident, "WAITING_APPROVAL", {"action_intent": fresh, "h2b_rounds": 1}
    )
    assert controls.decision(fresh["id"]) is None
    with pytest.raises(ControlConflict), controls.dispatch_guard(ident, fresh, run):
        pytest.fail("Old approval authorized a new action")
    barrier = Barrier(2)

    def approve():
        barrier.wait(timeout=5)
        try:
            controls.decide(
                ident,
                run_id=run,
                action_id=fresh["id"],
                intent_hash=digest(fresh),
                decision="approve",
                reason="race",
                actor="test",
            )
            return "approved"
        except ControlConflict:
            return "stopped"

    def stop():
        barrier.wait(timeout=5)
        return controls.stop(ident, "race stop", "test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(approve)
        stopping = pool.submit(stop)
        assert approval.result(timeout=10) in {"approved", "stopped"}
        assert stopping.result(timeout=10)["requested_at"]
    with pytest.raises(OperatorStop), controls.dispatch_guard(ident, fresh, run):
        pytest.fail("Dispatch after accepted stop")
