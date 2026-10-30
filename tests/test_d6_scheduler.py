from types import SimpleNamespace

import pytest

from benchmarks.d6 import run


def fake_environment(monkeypatch, tmp_path, remaining=10):
    work = tmp_path / "work"
    work.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    (private / "opened.json").write_text("{}")
    task = {
        "id": "holdout_01__B0__0",
        "case_id": "holdout_01",
        "variant": "B0",
        "repeat": 0,
    }
    monkeypatch.setattr(run, "WORK", work)
    monkeypatch.setattr(run, "PRIVATE", private)
    monkeypatch.setattr(
        run, "verify_protocol", lambda: {"image_id": "image", "tasks": [task]}
    )
    monkeypatch.setattr(run.diagnose, "image_provenance", lambda: {"image_id": "image"})
    summary = {
        "limit": 100,
        "used_slots": 100 - remaining,
        "remaining_slots": remaining,
        "attempts": [],
    }
    monkeypatch.setattr(
        run, "ModelBudget", lambda *a: SimpleNamespace(summary=lambda: summary)
    )
    monkeypatch.setattr(
        run.httpx, "get", lambda *a, **k: SimpleNamespace(json=lambda: summary)
    )
    return work, task


def test_finished_physical_task_is_not_reexecuted(monkeypatch, tmp_path):
    work, task = fake_environment(monkeypatch, tmp_path)
    result = work / "e2e" / "preserved"
    result.mkdir(parents=True)
    (result / "result.json").write_text("{}")
    run.save(
        work / "state.json",
        {
            "status": "running",
            "tasks": {task["id"]: {"status": "finished", "result_id": "preserved"}},
            "B1": {},
        },
    )
    monkeypatch.setattr(
        run.episode, "run", lambda *a, **k: pytest.fail("Repeated physical trial")
    )
    state = run.run("ignored")
    assert state["tasks"][task["id"]]["result_id"] == "preserved"


def test_uncertain_task_never_retries_automatically(monkeypatch, tmp_path):
    work, task = fake_environment(monkeypatch, tmp_path)
    run.save(
        work / "state.json",
        {"status": "running", "tasks": {task["id"]: {"status": "running"}}, "B1": {}},
    )
    monkeypatch.setattr(run.episode, "run", lambda *a, **k: pytest.fail("Blind retry"))
    with pytest.raises(ValueError, match="reconciliation"):
        run.run("ignored")


def test_environment_failure_is_preserved_and_stops(monkeypatch, tmp_path):
    _work, task = fake_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(
        run.episode,
        "run",
        lambda *a, **k: {
            "id": "environment-failed",
            "completed": False,
            "cleanup": {"passed": True},
        },
    )
    state = run.run("ignored")
    assert state["status"] == "awaiting_environment_review"
    assert state["tasks"][task["id"]]["result_id"] == "environment-failed"


def test_insufficient_budget_never_starts_paid_trial(monkeypatch, tmp_path):
    _work, task = fake_environment(monkeypatch, tmp_path, remaining=3)
    task["variant"] = "B3"
    monkeypatch.setattr(
        run.episode, "run", lambda *a, **k: pytest.fail("Overspent ledger")
    )
    assert run.run("ignored")["status"] == "awaiting_next_ledger"
