import copy

import pytest
from cloudsre.experiment_variants import settings

from benchmarks.d5 import continue_batch as continuation
from benchmarks.d5 import matrix


def approval():
    return {
        "batch_id": "b2",
        "limit": 20,
        "user_approved": True,
        "approved_at": "2026-01-01T00:00:00Z",
        "source_reply": "Synthetic unit-test approval; never a real runtime authorization",
    }


class FakeBudget:
    def __init__(self, limit=20):
        self.limit = limit
        self.attempts = []

    def summary(self):
        return {
            "limit": self.limit,
            "used_slots": len(self.attempts),
            "remaining_slots": self.limit - len(self.attempts),
            "attempts": copy.deepcopy(self.attempts),
        }


def scenario():
    tasks = [
        {
            "id": name,
            "case_id": name,
            "variant": "B1",
            "repeat_index": 0,
            "protocol": settings("B1"),
        }
        for name in ("finished", "next", "last")
    ]
    plan = {"tasks": tasks}
    state = {
        "status": "awaiting_budget",
        "plan_sha256": matrix.sha(plan),
        "tasks": {
            "finished": {"status": "finished", "result_id": "original-batch1-result"},
            "next": {"status": "pending"},
            "last": {"status": "pending"},
        },
    }
    first = {
        "available": True,
        "limit": 100,
        "used_slots": 100,
        "remaining_slots": 0,
        "attempts": [{"state": "finished"}],
    }
    return plan, state, first


@pytest.mark.parametrize(
    "change",
    [
        {"user_approved": False},
        {"user_approved": "true"},
        {"batch_id": "b1"},
        {"limit": 101},
        {"limit": 0},
        {"limit": True},
        {"source_reply": ""},
        {"approved_at": "2100-01-01T00:00:00Z"},
        {"approved_at": "2026-01-01T00:00:00"},
    ],
)
def test_d5_continuation_refuses_missing_or_invalid_explicit_user_authorization(change):
    value = {**approval(), **change}
    with pytest.raises(ValueError):
        continuation.authorization(value)


def test_d5_missing_approval_file_cannot_initialize_any_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(
        continuation,
        "ModelBudget",
        lambda *args: pytest.fail(
            "Missing user authorization must not construct a budget"
        ),
    )
    with pytest.raises(FileNotFoundError):
        continuation.run(tmp_path / "no-user-approval.json")
    assert not list(tmp_path.iterdir())


def test_d5_explicit_400_cumulative_approval_allows_only_100_in_second_batch():
    approved = {**approval(), "limit": 100, "global_total_limit": 400}
    assert continuation.authorization(approved) == approved
    for values in (
        {"global_total_limit": 199},
        {"global_total_limit": 401},
        {"global_total_limit": True},
        {"limit": 400},
    ):
        with pytest.raises(ValueError):
            continuation.authorization({**approved, **values})


def test_d5_runner_accepts_only_reconstructible_two_site_adapter():
    original = (
        continuation.OLD_SIGNATURE
        + "\n    # frozen logic\n"
        + continuation.OLD_BUDGET
        + "\n"
    ).encode()
    current = continuation.adapter(original)
    expected = {
        continuation.RUNNER: continuation.sha(original),
        "baseline": "unchanged",
    }
    actual = {continuation.RUNNER: continuation.sha(current), "baseline": "unchanged"}
    proof = continuation.verify_sources(expected, actual, original, current)
    assert proof["old_runner_sha256"] == continuation.sha(original)
    assert proof["new_runner_sha256"] == continuation.sha(current)
    for bad in (current + b"# one extra change\n", original):
        with pytest.raises(ValueError, match="two-site"):
            continuation.verify_sources(expected, actual, original, bad)
    with pytest.raises(ValueError, match="other source"):
        continuation.verify_sources(
            expected, {**actual, "baseline": "changed"}, original, current
        )


def test_d5_continuation_refuses_changed_snapshots_or_per_run_settings():
    capture = {"snapshot_id": "frozen", "records_sha256": {"logs": "hash"}}
    task = {
        "case_id": "case",
        "snapshot": capture,
        "variant": "B1",
        "protocol": settings("B1"),
    }
    plan = {"tasks": [task]}
    continuation.verify_frozen(plan, {"case": capture})
    with pytest.raises(ValueError, match="Frozen observation"):
        continuation.verify_frozen(plan, {"case": {**capture, "snapshot_id": "other"}})
    task["protocol"] = {**task["protocol"], "max_provider_attempts": 9}
    with pytest.raises(ValueError, match="per-run"):
        continuation.verify_frozen(plan, {"case": capture})


def test_d5_continuation_resumes_only_pending_original_tasks_and_writes_batch_sidecars():
    plan, state, first = scenario()
    before_plan, before_first = copy.deepcopy(plan), copy.deepcopy(first)
    old = copy.deepcopy(state["tasks"]["finished"])
    budget = FakeBudget()
    calls, results, sidecars, saved = [], [{"id": "original-batch1-result"}], [], []

    def trial(case, variant, repeat, *, budget_override):
        assert budget_override is budget
        calls.append(case)
        budget.attempts.append({"state": "finished", "run_id": case})
        result = {"id": "b2-" + case, "provider_http_attempts": 1}
        results.append(result)
        return result

    continued = continuation.resume(
        plan,
        state,
        approved=approval(),
        budget=budget,
        health=budget.summary(),
        first=first,
        trial_fn=trial,
        save_fn=lambda value: saved.append(copy.deepcopy(value)),
        results_fn=lambda: results,
        sidecar_fn=sidecars.append,
    )
    assert calls == ["next", "last"] and continued["status"] == "finished"
    assert state["tasks"]["finished"] == old
    assert plan == before_plan and first == before_first
    assert [row["run_id"] for row in sidecars] == ["b2-next", "b2-last"]
    assert all(
        row["batch_id"] == "b2" and row["plan_sha256"] == matrix.sha(plan)
        for row in sidecars
    )
    assert saved and len(budget.attempts) == 2
    # Running the completed schedule again consumes nothing and emits no sidecar.
    continuation.resume(
        plan,
        state,
        approved=approval(),
        budget=budget,
        health=budget.summary(),
        first=first,
        trial_fn=trial,
        save_fn=lambda value: None,
        results_fn=lambda: results,
        sidecar_fn=sidecars.append,
    )
    assert calls == ["next", "last"] and len(sidecars) == 2


@pytest.mark.parametrize(
    "failure",
    ["first_inflight", "second_inflight", "wrong_proxy", "wrong_limit", "running_task"],
)
def test_d5_continuation_preflight_rejects_before_any_trial(failure):
    plan, state, first = scenario()
    budget = FakeBudget()
    health = budget.summary()
    if failure == "first_inflight":
        first["attempts"].append({"state": "reserved_unknown"})
    elif failure == "second_inflight":
        budget.attempts.append({"state": "reserved_unknown"})
    elif failure == "wrong_proxy":
        health["limit"] = 100
    elif failure == "wrong_limit":
        budget.limit = 21
    elif failure == "running_task":
        state["tasks"]["next"]["status"] = "running"
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        continuation.resume(
            plan,
            state,
            approved=approval(),
            budget=budget,
            health=health,
            first=first,
            trial_fn=lambda *args, **kwargs: pytest.fail(
                "Preflight failure must not call a model"
            ),
            save_fn=lambda value: pytest.fail(
                "Preflight failure must not alter the schedule"
            ),
            results_fn=list,
            sidecar_fn=lambda value: pytest.fail(
                "Preflight failure must not fabricate a sidecar"
            ),
        )
    assert state == before


def test_d5_sidecar_failure_preserves_paid_result_and_refuses_implicit_rerun():
    plan, state, first = scenario()
    budget = FakeBudget()
    results, calls = [{"id": "original-batch1-result"}], []

    def trial(case, variant, repeat, *, budget_override):
        calls.append(case)
        budget.attempts.append({"state": "finished"})
        value = {"id": "completed-but-sidecar-failed", "provider_http_attempts": 1}
        results.append(value)
        return value

    def fail(value):
        raise OSError("Synthetic sidecar I/O error")

    with pytest.raises(OSError):
        continuation.resume(
            plan,
            state,
            approved=approval(),
            budget=budget,
            health=budget.summary(),
            first=first,
            trial_fn=trial,
            save_fn=lambda value: None,
            results_fn=lambda: results,
            sidecar_fn=fail,
        )
    assert state["status"] == "interrupted_unresolved"
    assert len(calls) == len(budget.attempts) == 1 and len(results) == 2
    with pytest.raises(ValueError, match="manual reconciliation"):
        continuation.resume(
            plan,
            state,
            approved=approval(),
            budget=budget,
            health=budget.summary(),
            first=first,
            trial_fn=trial,
            save_fn=lambda value: None,
            results_fn=lambda: results,
            sidecar_fn=fail,
        )
    assert len(calls) == 1
