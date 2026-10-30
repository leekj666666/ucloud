"""Durable serial formal schedule. Failures are never silently retried."""

import argparse
import fcntl
import json
import time

import httpx
from cloudsre.model_budget import ModelBudget

from benchmarks.d5.continue_batch import no_inflight

from . import diagnose, episode
from .protocol import PRIVATE, WORK, verify_protocol


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def _run(database, max_tasks=None):
    plan = verify_protocol()
    if not (PRIVATE / "opened.json").exists():
        raise ValueError("Holdout has not been explicitly opened")
    actual = diagnose.image_provenance()
    if actual["image_id"] != plan["image_id"]:
        raise ValueError("Frozen image changed")
    budget = ModelBudget(database, 100)
    no_inflight(budget.summary())
    health = httpx.get("http://localhost:18090/health", timeout=10).json()
    if any(
        health.get(k) != budget.summary()[k]
        for k in ("limit", "used_slots", "remaining_slots")
    ):
        raise ValueError("Active proxy and ledger differ")
    path = WORK / "state.json"
    state = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "status": "ready",
            "tasks": {t["id"]: {"status": "pending"} for t in plan["tasks"]},
            "B1": {},
        }
    )
    if set(state["tasks"]) != {t["id"] for t in plan["tasks"]}:
        raise ValueError("Schedule identities changed")
    if any(x["status"] != "finished" for x in state["B1"].values()):
        raise ValueError("Interrupted B1 diagnosis requires explicit reconciliation")
    for previous in state["B1"].values():
        if not (WORK / "results" / (previous["result_id"] + ".json")).exists():
            raise ValueError("Missing preserved B1 result")
    finished_this_call = 0
    for task in plan["tasks"]:
        verify_protocol()
        if diagnose.image_provenance()["image_id"] != plan["image_id"]:
            raise ValueError("Diagnostic image changed during schedule")
        entry = state["tasks"][task["id"]]
        if entry["status"] == "finished":
            if not (WORK / "e2e" / entry["result_id"] / "result.json").exists():
                raise ValueError("Missing preserved result")
        elif entry["status"] == "pending":
            if max_tasks is not None and finished_this_call >= max_tasks:
                break
            if budget.summary()["remaining_slots"] < 4 and task["variant"] != "B0":
                state["status"] = "awaiting_next_ledger"
                save(path, state)
                return state
            entry.update(status="running", started_unix=time.time())
            state["status"] = "running"
            save(path, state)
            try:
                result = episode.run(
                    task["case_id"],
                    task["variant"],
                    task["repeat"],
                    diagnostic=lambda c, v, i, **kw: diagnose.trial(
                        c, v, i, budget_override=budget, **kw
                    ),
                )
            except BaseException:
                state["status"] = "awaiting_explicit_reconciliation"
                save(path, state)
                raise
            entry.update(status="finished", result_id=result["id"])
            finished_this_call += 1
            save(path, state)
            if not result.get("completed") or not result.get("cleanup", {}).get(
                "passed"
            ):
                state["status"] = "awaiting_environment_review"
                save(path, state)
                return state
        else:
            raise ValueError("Interrupted task requires explicit reconciliation")
        if task["variant"] == "B3" and task["id"] not in state["B1"]:
            if budget.summary()["remaining_slots"] < 1:
                state["status"] = "awaiting_next_ledger"
                save(path, state)
                return state
            original = json.loads(
                (WORK / "e2e" / entry["result_id"] / "result.json").read_text()
            )
            capture = json.loads(
                (
                    PRIVATE / "captures" / ("e2e-" + original["id"]) / "capture.json"
                ).read_text()
            )
            state["B1"][task["id"]] = {
                "status": "running",
                "source_episode": original["id"],
            }
            save(path, state)
            output = diagnose.trial(
                task["case_id"],
                "B1",
                task["repeat"],
                capture_override=capture,
                budget_override=budget,
            )
            state["B1"][task["id"]].update(status="finished", result_id=output["id"])
            save(path, state)
    if (
        all(x["status"] == "finished" for x in state["tasks"].values())
        and len(state["B1"]) == 60
        and all(x["status"] == "finished" for x in state["B1"].values())
    ):
        state["status"] = "finished"
    save(path, state)
    return state


def run(database, max_tasks=None):
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "schedule.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run(database, max_tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--max-tasks", type=int)
    args = parser.parse_args()
    print(json.dumps(run(args.database, args.max_tasks)))
