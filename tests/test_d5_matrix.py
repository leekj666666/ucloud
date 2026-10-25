import copy
import json

import pytest
from cloudsre.durable import digest
from cloudsre.experiment_views import KINDS

from benchmarks.d5.analyze import analyze, live_summary, markdown
from benchmarks.d5.catalog import DEV, VALIDATION
from benchmarks.d5.matrix import build_plan, execute, reconcile, sha, snapshot_identity


def plan_and_state():
    plan = build_plan(
        {
            case.id: {"snapshot_id": "snapshot-" + case.id}
            for case in (*DEV, *VALIDATION)
        },
        {"protocol": "fixed"},
    )
    state = {
        "plan_sha256": sha(plan),
        "status": "ready",
        "tasks": {task["id"]: {"status": "pending"} for task in plan["tasks"]},
    }
    return plan, state


def test_schedule_contains_all_cases_and_is_frozen_before_outcomes():
    plan, _ = plan_and_state()
    again, _ = plan_and_state()
    assert plan["tasks"] == again["tasks"]
    assert len(plan["tasks"]) == 76
    assert len({task["id"] for task in plan["tasks"]}) == 76
    assert all(task["variant"] == "B0" for task in plan["tasks"][:20])
    assert all(task["split"] == "validation" for task in plan["tasks"][20:])
    assert {task["case_id"] for task in plan["tasks"][:20]} == {
        case.id for case in (*DEV, *VALIDATION)
    }
    assert all("holdout" not in task["case_id"] for task in plan["tasks"])
    for variant in (
        "B1",
        "B2",
        "B3",
        "A_D4_compact",
        "A_neutral_raw",
        "A_D4_raw",
        "A_H1_off",
    ):
        assert sum(task["variant"] == variant for task in plan["tasks"]) == 8
    with pytest.raises(ValueError, match="exactly"):
        build_plan({"dev_01": {}}, {})


class Budget:
    def __init__(self, remaining):
        self.remaining = remaining

    def summary(self):
        return {"remaining_slots": self.remaining}


def test_schedule_preserves_all_tasks_at_reserve_and_total_budget_stop():
    plan, state = plan_and_state()
    calls = []

    def run(case_id, variant, repeat):
        calls.append((case_id, variant, repeat))
        return {"id": "result-" + case_id}

    saved = []
    result = execute(
        plan,
        state,
        budget=Budget(4),
        trial_fn=run,
        save_fn=lambda value: saved.append(copy.deepcopy(value)),
        results_fn=list,
        reserve_attempts=4,
    )
    assert result["status"] == "scheduling_reserve_reached"
    assert len(calls) == 20 and all(call[1] == "B0" for call in calls)
    assert sum(entry["status"] == "pending" for entry in state["tasks"].values()) == 56
    assert len(plan["tasks"]) == 76
    result = execute(
        plan,
        state,
        budget=Budget(0),
        trial_fn=run,
        save_fn=lambda value: None,
        results_fn=list,
    )
    assert result["status"] == "awaiting_budget" and len(calls) == 20
    assert saved[-1]["plan_sha256"] == sha(plan)


def test_failed_outputs_are_finished_not_implicitly_retried_and_resume_skips_them():
    plan, state = plan_and_state()
    plan["tasks"] = plan["tasks"][:2]
    state["plan_sha256"] = sha(plan)
    calls = []

    def failed(case_id, variant, repeat):
        calls.append(case_id)
        return {"id": "failed-" + case_id, "output_valid": False}

    kwargs = {
        "budget": Budget(100),
        "trial_fn": failed,
        "save_fn": lambda value: None,
        "results_fn": list,
    }
    execute(plan, state, **kwargs)
    execute(plan, state, **kwargs)
    assert len(calls) == 2
    assert state["status"] == "finished"
    assert all(
        state["tasks"][task["id"]]["status"] == "finished" for task in plan["tasks"]
    )
    plan["schedule_seed"] += 1
    with pytest.raises(ValueError, match="modified"):
        execute(plan, state, **kwargs)


def test_interruption_reconciles_only_unique_matching_raw_result():
    plan, _ = plan_and_state()
    task = plan["tasks"][0]
    entry = {"status": "running", "dispatch_started_unix": 100}
    raw = {
        "id": "unique",
        "case_id": task["case_id"],
        "variant": task["variant"],
        "repeat_index": 0,
        "snapshot_id": task["snapshot"]["snapshot_id"],
        "started_unix": 101,
    }
    assert reconcile(task, entry, [raw, {**raw, "id": "old", "started_unix": 99}])
    assert entry["result_id"] == "unique"
    unresolved = {"status": "running", "dispatch_started_unix": 100}
    assert not reconcile(task, unresolved, [raw, {**raw, "id": "duplicate"}])
    assert unresolved["status"] == "interrupted_unresolved"


def attach_result(
    plan,
    state,
    task_id,
    *,
    valid=True,
    root=True,
    disposition=True,
    tokens=12,
    unknown=0,
):
    task = next(task for task in plan["tasks"] if task["id"] == task_id)
    result = {
        "id": "result-" + task_id,
        "case_id": task["case_id"],
        "variant": task["variant"],
        "repeat_index": 0,
        "snapshot_id": task["snapshot"]["snapshot_id"],
        "recovery_measured": False,
        "output_valid": valid,
        "provider_http_attempts": 1,
        "known_total_tokens": tokens,
        "known_usage_attempts": 1 - unknown,
        "unknown_usage_attempts": unknown,
        "scoring": {
            "root_action_proxy_correct": root,
            "disposition_correct": disposition,
        },
    }
    state["tasks"][task_id].update(status="finished", result_id=result["id"])
    return result


def test_analysis_keeps_planned_missing_failures_and_unsupported_denominators():
    plan, state = plan_and_state()
    first = attach_result(
        plan,
        state,
        "val_01__B1",
        valid=False,
        root=False,
        disposition=False,
        tokens=0,
        unknown=1,
    )
    second = attach_result(plan, state, "val_07__B1", root=None, disposition=True)
    setup_failure = {"id": "setup", "output_valid": False, "provider_http_attempts": 0}
    report = analyze(plan, state, [first, second, setup_failure])
    baseline = report["diagnostic_validation"]["main_systems"]["per_variant"]["B1"]
    assert baseline["planned_trials"] == 8 and baseline["observed_trials"] == 2
    assert baseline["missing_trials"] == 6
    assert baseline["metrics"]["output_valid"]["success"] == 1
    root = baseline["metrics"]["root_action_proxy_correct"]
    assert root == {
        "planned": 7,
        "observed": 1,
        "missing": 6,
        "success": 0,
        "failure": 1,
        "observed_rate": 0.0,
    }
    assert baseline["unknown_usage_attempts"] == 1 and baseline["cost_amount"] is None
    assert report["unscheduled_zero_call_invalid_trials"] == 1
    assert report["live_end_to_end"]["external_recovery"] == 0
    assert "usage 未知" in markdown(report)


def test_paired_counts_do_not_drop_invalid_outputs_or_count_missing_as_failures():
    plan, state = plan_and_state()
    results = [
        attach_result(plan, state, "val_01__B1", root=True),
        attach_result(
            plan, state, "val_01__B3", valid=False, root=False, disposition=False
        ),
        attach_result(plan, state, "val_02__B1", root=True),
    ]
    report = analyze(plan, state, results)
    pairs = report["diagnostic_validation"]["main_systems"]["paired_comparisons"]
    pair = next(
        row
        for row in pairs
        if row["left"] == "B1"
        and row["right"] == "B3"
        and row["metric"] == "root_action_proxy_correct"
    )
    assert pair["planned_pairs"] == 7 and pair["observed_pairs"] == 1
    assert pair["left_wins"] == 1 and pair["missing_pairs"] == 6


def test_analyzer_refuses_live_as_replay_or_changed_case_snapshot():
    plan, state = plan_and_state()
    result = attach_result(plan, state, "val_01__B1")
    result["recovery_measured"] = True
    with pytest.raises(ValueError, match="recovery"):
        analyze(plan, state, [result])
    result["recovery_measured"] = False
    result["snapshot_id"] = "different"
    with pytest.raises(ValueError, match="snapshot"):
        analyze(plan, state, [result])
    with pytest.raises(ValueError, match="non-live"):
        live_summary([result])


def test_snapshot_freeze_detects_identity_only_tampering_and_missing_incident_hash(
    tmp_path,
):
    snapshot = tmp_path / "snapshots" / "frozen"
    snapshot.mkdir(parents=True)
    hashes = {}
    for kind in KINDS:
        data = {"observation": kind}
        record = {
            "kind": kind,
            "id": "id-" + kind,
            "data": data,
            "content_hash": digest(data),
        }
        (snapshot / (kind + ".json")).write_text(json.dumps(record))
        hashes[kind] = digest(record)
    incident = {"namespace": "sre-demo", "title": "Neutral incident"}
    (snapshot / "incident.json").write_text(json.dumps(incident))
    capture = {
        "snapshot_id": "frozen",
        "passed": True,
        "scoring_ready": True,
        "records_sha256": hashes,
    }
    index = {"dev_01": capture}
    with pytest.raises(ValueError, match="incident"):
        snapshot_identity("dev_01", index, tmp_path)
    capture["incident_sha256"] = digest(incident)
    assert snapshot_identity("dev_01", index, tmp_path)["records_sha256"] == hashes
    record = json.loads((snapshot / "logs.json").read_text())
    record["id"] = "different-id-with-identical-data"
    (snapshot / "logs.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="complete"):
        snapshot_identity("dev_01", index, tmp_path)


def test_live_success_requires_root_before_and_after_independent_window():
    result = {
        "experiment_kind": "live_end_to_end",
        "diagnostic_replay_only": False,
        "external_recovery": True,
        "environment_valid": True,
        "root_stable": True,
        "root_before_window": {"passed": True},
        "root_after_window": {"passed": True},
        "quality": {"passed": True},
    }
    assert live_summary([result])["external_recovery"] == 1
    result["root_after_window"]["passed"] = False
    with pytest.raises(ValueError, match="root/window"):
        live_summary([result])


def test_h1_incremental_prompt_reports_all_and_predefined_distractor_subgroups():
    plan, state = plan_and_state()
    results = [
        attach_result(plan, state, "val_01__A_D4_compact", root=True),
        attach_result(plan, state, "val_01__A_H1_off", root=False),
        attach_result(plan, state, "val_02__A_D4_compact", valid=False, root=False),
        attach_result(plan, state, "val_02__A_H1_off", root=True),
        attach_result(plan, state, "val_07__A_D4_compact", root=None),
        attach_result(plan, state, "val_07__A_H1_off", root=None),
    ]
    results[1]["scoring"]["disposition"] = "blocked_by_common_policy"
    results[4]["proposal"] = {"action": "escalate"}
    results[5]["proposal"] = {"action": "escalate"}
    report = analyze(plan, state, results)
    h1 = report["diagnostic_validation"]["incremental_H1_prompt"]
    subgroups = h1["predefined_subgroups"]
    assert {
        name: len(value["planned_case_ids"]) for name, value in subgroups.items()
    } == {
        "all_8": 8,
        "distractor_true": 5,
        "distractor_false": 3,
    }
    root_pair = next(
        row
        for row in h1["paired_comparisons"]
        if row["metric"] == "root_action_proxy_correct"
    )
    assert root_pair["planned_pairs"] == 7
    assert root_pair["observed_pairs"] == 2
    assert root_pair["left_wins"] == 1 and root_pair["right_wins"] == 1
    for subgroup, planned, left_wins, right_wins in (
        ("distractor_true", 5, 1, 0),
        ("distractor_false", 2, 0, 1),
    ):
        pair = next(
            row
            for row in subgroups[subgroup]["paired_comparisons"]
            if row["metric"] == "root_action_proxy_correct"
        )
        assert (pair["planned_pairs"], pair["left_wins"], pair["right_wins"]) == (
            planned,
            left_wins,
            right_wins,
        )
    valid_pair = next(
        row
        for row in h1["paired_comparisons"]
        if row["metric"] == "proposal_acceptable"
    )
    assert valid_pair["observed_pairs"] == 3 and valid_pair["tie_success"] == 1
    assert valid_pair["left_wins"] == 1 and valid_pair["right_wins"] == 1
    text = markdown(report)
    assert "H1 增量提示消融" in text and "distractor_true" in text
    assert "不能称为 H1 单因素" in text


def test_h1_subgroup_refuses_missing_predeclared_distractor_label():
    plan, state = plan_and_state()
    task = next(task for task in plan["tasks"] if task["id"] == "val_01__A_H1_off")
    del task["distractor_present"]
    state["plan_sha256"] = sha(plan)
    with pytest.raises(ValueError, match="predeclared"):
        analyze(plan, state, [])
