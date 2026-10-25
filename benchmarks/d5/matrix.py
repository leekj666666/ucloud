"""Evaluator-only frozen, resumable D5 schedule; never retries failed trials."""

import argparse
import hashlib
import json
import os
import random
import time
from pathlib import Path

from cloudsre.durable import digest
from cloudsre.experiment_variants import settings
from cloudsre.model_budget import ModelBudget

from .catalog import DEV, VALIDATION
from .live import PRIVATE, ROOT, WORK, exclusive

PAID_VARIANTS = (
    "B1",
    "B2",
    "B3",
    "A_D4_compact",
    "A_neutral_raw",
    "A_D4_raw",
    "A_H1_off",
)
PROTOCOL_FILES = (
    "backend/cloudsre/diagnostic_protocol.py",
    "backend/cloudsre/experiment_variants.py",
    "backend/cloudsre/experiment_prepare.py",
    "backend/cloudsre/experiment_views.py",
    "backend/cloudsre/diagnostic_rules.py",
    "backend/cloudsre/evidence_v3.py",
    "backend/cloudsre/policy_v3.py",
    "backend/cloudsre/actions.py",
    "backend/cloudsre/model_budget.py",
    "benchmarks/d5/diagnose.py",
    "benchmarks/d5/catalog.py",
    "infra/.generated/d4/baseline.json",
    "diagnosis/runner_d5.py",
)


def sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def protocol_hashes(root=ROOT):
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in PROTOCOL_FILES
    }


def snapshot_identity(case_id, index, work=WORK):
    from cloudsre.experiment_views import load_records

    capture = index.get(case_id, {})
    if not capture.get("passed") or not capture.get("scoring_ready"):
        raise ValueError(
            "All 20 dev/validation captures must be scoring-ready: " + case_id
        )
    snapshot = work / "snapshots" / capture["snapshot_id"]
    records = load_records(snapshot)
    record_hashes = {kind: digest(record) for kind, record in records.items()}
    if capture.get("records_sha256") != record_hashes:
        raise ValueError("Frozen complete snapshot record hash mismatch: " + case_id)
    incident_hash = digest(json.loads((snapshot / "incident.json").read_text()))
    if capture.get("incident_sha256") != incident_hash:
        raise ValueError("Frozen incident hash missing or mismatched: " + case_id)
    return {
        "snapshot_id": capture["snapshot_id"],
        "records_sha256": record_hashes,
        "incident_sha256": incident_hash,
    }


def frozen_captures(index):
    frozen = json.loads((PRIVATE / "frozen-snapshots.json").read_text())["captures"]
    captures = {
        case.id: snapshot_identity(case.id, index) for case in (*DEV, *VALIDATION)
    }
    if captures != frozen:
        raise ValueError("Dataset differs from the separate pre-experiment freeze")
    return captures


def build_plan(captures, source_hashes, seed=20261025, dev=DEV, validation=VALIDATION):
    """Free and paid phases are separately shuffled before any run is observed."""
    expected = {case.id for case in (*dev, *validation)}
    if set(captures) != expected:
        raise ValueError(
            "Frozen captures must cover exactly the planned dev/validation cases"
        )
    generator = random.Random(seed)
    free = [(case, "B0") for case in (*dev, *validation)]
    paid = [(case, variant) for case in validation for variant in PAID_VARIANTS]
    generator.shuffle(free)
    generator.shuffle(paid)
    tasks = []
    for case, variant in (*free, *paid):
        tasks.append(
            {
                "id": case.id + "__" + variant,
                "case_id": case.id,
                "split": case.split,
                "family": case.family,
                "distractor_present": bool(case.distractor),
                "expected_disposition": case.expected_disposition,
                "supported_action": case.family != "unsupported",
                "variant": variant,
                "repeat_index": 0,
                "phase": "rules" if variant == "B0" else "paid_validation",
                "protocol": settings(variant),
                "snapshot": captures[case.id],
            }
        )
    return {
        "schema_version": 1,
        "schedule_seed": seed,
        "model_seed_supported": False,
        "source_hashes": source_hashes,
        "created_unix": time.time(),
        "scope": "D5 diagnostic screening, not D6 holdout or live recovery",
        "comparison_groups": {
            "main_systems": ["B0", "B1", "B2", "B3"],
            "incremental_H1_prompt": ["A_D4_compact", "A_H1_off"],
            "single_call_factorial": [
                "B1",
                "A_D4_compact",
                "A_neutral_raw",
                "A_D4_raw",
            ],
        },
        "tasks": tasks,
    }


def prepare(directory, seed=20261025):
    with exclusive():
        return prepare_locked(directory, seed)


def prepare_locked(directory, seed):
    directory.mkdir(parents=True, exist_ok=True)
    plan_path, state_path = directory / "plan.json", directory / "state.json"
    if plan_path.exists() or state_path.exists():
        raise FileExistsError("A frozen schedule cannot be overwritten")
    index = json.loads((PRIVATE / "capture-index.json").read_text())
    captures = frozen_captures(index)
    plan = build_plan(captures, protocol_hashes(), seed)
    write_json(plan_path, plan)
    state = {
        "plan_sha256": sha(plan),
        "status": "ready",
        "tasks": {task["id"]: {"status": "pending"} for task in plan["tasks"]},
    }
    write_json(state_path, state)
    return plan, state


def reconcile(task, entry, results):
    """Only recover a unique already-written result after a scheduler interruption."""
    matches = [
        row
        for row in results
        if row.get("case_id") == task["case_id"]
        and row.get("variant") == task["variant"]
        and row.get("repeat_index") == task["repeat_index"]
        and row.get("snapshot_id") == task["snapshot"]["snapshot_id"]
        and row.get("started_unix", 0) >= entry["dispatch_started_unix"]
    ]
    if len(matches) != 1:
        entry.update(
            status="interrupted_unresolved",
            matching_result_ids=[row["id"] for row in matches],
        )
        return False
    entry.update(
        status="finished", result_id=matches[0]["id"], recovered_after_interrupt=True
    )
    return True


def execute(plan, state, *, budget, trial_fn, save_fn, results_fn, reserve_attempts=0):
    if state["plan_sha256"] != sha(plan):
        raise ValueError("Frozen schedule was modified")
    if not 0 <= reserve_attempts <= 100:
        raise ValueError("Invalid scheduling reserve")
    for task in plan["tasks"]:
        entry = state["tasks"][task["id"]]
        if entry["status"] == "finished":
            continue
        if entry["status"] == "running":
            if not reconcile(task, entry, results_fn()):
                state.update(
                    status="interrupted_unresolved", stopped_task_id=task["id"]
                )
                save_fn(state)
                return state
            save_fn(state)
            continue
        if entry["status"] != "pending":
            state.update(status=entry["status"], stopped_task_id=task["id"])
            save_fn(state)
            return state
        remaining = budget.summary()["remaining_slots"]
        needed = task["protocol"]["max_provider_attempts"]
        if needed and remaining - reserve_attempts < needed:
            state.update(
                status="awaiting_budget"
                if remaining < needed
                else "scheduling_reserve_reached",
                stopped_task_id=task["id"],
                remaining_slots=remaining,
                reserve_attempts=reserve_attempts,
                next_run_max_attempts=needed,
            )
            save_fn(state)
            return state
        entry.update(status="running", dispatch_started_unix=time.time())
        state.update(status="running")
        save_fn(state)
        try:
            result = trial_fn(task["case_id"], task["variant"], task["repeat_index"])
        except Exception as exc:
            # No implicit rerun: an exception may follow a paid HTTP request.
            entry.update(status="interrupted_unresolved", error_type=type(exc).__name__)
            state.update(status="interrupted_unresolved", stopped_task_id=task["id"])
            save_fn(state)
            raise
        entry.update(status="finished", result_id=result["id"])
        save_fn(state)
    state.update(status="finished", reserve_attempts=reserve_attempts)
    save_fn(state)
    return state


def run(directory, reserve_attempts=0):
    from .diagnose import trial

    with exclusive():
        return run_locked(directory, reserve_attempts, trial)


def run_locked(directory, reserve_attempts, trial):
    # Read the mutable checkpoint only after obtaining the same lock as capture
    # and live trials. Two launchers must never act on a stale pending state.
    plan = json.loads((directory / "plan.json").read_text())
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text())
    if protocol_hashes() != plan["source_hashes"]:
        raise ValueError("Diagnostic source changed after schedule freeze")
    index = json.loads((PRIVATE / "capture-index.json").read_text())
    captures = frozen_captures(index)
    for task in plan["tasks"]:
        if captures[task["case_id"]] != task["snapshot"]:
            raise ValueError("A frozen snapshot changed: " + task["case_id"])
        if settings(task["variant"]) != task["protocol"]:
            raise ValueError("Variant protocol changed after freeze")
    budget = ModelBudget(WORK / "budget-private/batch.sqlite", 100)
    return execute(
        plan,
        state,
        budget=budget,
        trial_fn=trial,
        save_fn=lambda current: write_json(state_path, current),
        results_fn=lambda: [
            json.loads(file.read_text()) for file in (WORK / "results").glob("*.json")
        ],
        reserve_attempts=reserve_attempts,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--directory", type=Path, default=WORK / "matrix")
    parser.add_argument("--seed", type=int, default=20261025)
    parser.add_argument("--reserve-attempts", type=int, default=0)
    args = parser.parse_args()
    if args.command == "prepare":
        plan, state = prepare(args.directory, args.seed)
        print(
            json.dumps(
                {
                    "status": state["status"],
                    "tasks": len(plan["tasks"]),
                    "plan_sha256": sha(plan),
                }
            )
        )
    else:
        state = run(args.directory, args.reserve_attempts)
        print(
            json.dumps({key: value for key, value in state.items() if key != "tasks"})
        )


if __name__ == "__main__":
    main()
