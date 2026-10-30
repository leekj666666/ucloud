from types import SimpleNamespace

import pytest

from benchmarks.d6 import budget


def test_rotation_refuses_when_existing_ledger_has_full_task_capacity(
    monkeypatch, tmp_path
):
    from contextlib import nullcontext

    monkeypatch.setattr(budget, "exclusive", nullcontext)
    monkeypatch.setattr(
        budget, "current", lambda: (tmp_path / "batch4.sqlite", {"host_pid": 123})
    )
    monkeypatch.setattr(
        budget,
        "ModelBudget",
        lambda *a: SimpleNamespace(summary=lambda: {"remaining_slots": 4}),
    )
    monkeypatch.setattr(
        budget.os, "kill", lambda *a: pytest.fail("Killed healthy usable proxy")
    )
    with pytest.raises(ValueError, match="still fits"):
        budget.rotate()
