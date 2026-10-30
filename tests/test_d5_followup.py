from benchmarks.d5.catalog import DEV, VALIDATION
from benchmarks.d5.followup import build_tasks


def test_followup_fixed_population_and_no_holdout():
    captures = {c.id: {"snapshot_id": c.id} for c in (*DEV, *VALIDATION)}
    tasks = build_tasks(captures)
    assert tasks == build_tasks(captures)
    assert len(tasks) == len({t["id"] for t in tasks}) == 28
    assert sum(t["variant"] == "H4_alias" for t in tasks) == 8
    assert all(t["split"] == "validation" for t in tasks if t["variant"] == "H4_alias")
    assert sum(t["variant"] == "H5_hybrid" for t in tasks) == 20
    assert all(t["protocol"]["max_provider_attempts"] == 4 for t in tasks)


def test_transport_failure_preserved_and_stops_future_dispatch():
    from types import SimpleNamespace

    from benchmarks.d5.followup import execute
    from benchmarks.d5.matrix import sha

    tasks = [
        {
            "id": str(i),
            "case_id": str(i),
            "variant": "H4_alias",
            "repeat_index": 0,
            "protocol": {"max_provider_attempts": 4},
        }
        for i in range(2)
    ]
    plan = {"tasks": tasks}
    state = {
        "plan_sha256": sha(plan),
        "tasks": {str(i): {"status": "pending"} for i in range(2)},
    }
    calls = []

    def diagnostic(*args):
        calls.append(args)
        return {"id": "failed-transport"}

    execute(
        plan,
        state,
        SimpleNamespace(summary=lambda: {"remaining_slots": 96}),
        diagnostic,
        lambda: None,
        lambda r: True,
    )
    assert len(calls) == 1
    assert state["status"] == "awaiting_network_review"
    assert state["tasks"]["0"] == {
        "status": "finished",
        "result_id": "failed-transport",
        "dispatch_started_unix": state["tasks"]["0"]["dispatch_started_unix"],
    }
    assert state["tasks"]["1"]["status"] == "pending"
