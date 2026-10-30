"""Predeclared D5 alias/hybrid screening; preserves all prior failed trials."""

import argparse
import json
import random
import time

import httpx
from cloudsre.experiment_variants import settings
from cloudsre.model_budget import ModelBudget

from . import matrix
from .catalog import DEV, VALIDATION
from .continue_batch import authorization, no_inflight
from .diagnose import trial
from .live import PRIVATE, WORK, exclusive


def build_tasks(captures):
    pairs = [(c, v) for c in VALIDATION for v in ("B3", "H4_alias")]
    pairs += [(c, "H5_hybrid") for c in (*DEV, *VALIDATION)]
    random.Random(20261029).shuffle(pairs)
    return [
        {
            "id": c.id + "__" + v,
            "case_id": c.id,
            "split": c.split,
            "variant": v,
            "repeat_index": 100,
            "protocol": {**settings(v), "thinking": "disabled", "version": "D5-C1"},
            "snapshot": captures[c.id],
        }
        for c, v in pairs
    ]


def transport_failed(result):
    path = WORK / "diagnostics" / result["id"] / "provider-attempts.json"
    return any(
        row.get("error_type") and row.get("http_status") is None
        for row in json.loads(path.read_text())
    )


def run(prepare_only=False):
    approved = authorization(
        json.loads((PRIVATE / "batch2-authorization.json").read_text())
    )
    with exclusive():
        directory = WORK / "plan-c"
        directory.mkdir(exist_ok=True)
        captures = matrix.frozen_captures(
            json.loads((PRIVATE / "capture-index.json").read_text())
        )
        source = matrix.protocol_hashes()
        plan_path, state_path = directory / "plan.json", directory / "state.json"
        if not plan_path.exists():
            plan = {
                "created_unix": time.time(),
                "source_hashes": source,
                "schedule_seed": 20261029,
                "tasks": build_tasks(captures),
                "scope": "Exploratory D5 screening after initial outcomes; not confirmatory or D6",
                "comparisons": {
                    "H4_alias": "D5-C1 B3 same validation snapshot",
                    "H5_hybrid": "B0 on dev; B0 and D5-C1 B3 on validation",
                },
                "rule": "No automatic retries; stop on first upstream transport failure; retain all denominators",
            }
            matrix.write_json(plan_path, plan)
            matrix.write_json(
                state_path,
                {
                    "plan_sha256": matrix.sha(plan),
                    "status": "ready",
                    "tasks": {t["id"]: {"status": "pending"} for t in plan["tasks"]},
                },
            )
        plan, state = (
            json.loads(plan_path.read_text()),
            json.loads(state_path.read_text()),
        )
        if plan["source_hashes"] != source or plan["tasks"] != build_tasks(captures):
            raise ValueError("Frozen followup inputs changed")
        if prepare_only:
            return state
        budget = ModelBudget(WORK / "budget-private/batch2.sqlite", approved["limit"])
        no_inflight(budget.summary())
        health = httpx.get("http://localhost:18090/health", timeout=10).json()
        if any(
            health.get(k) != budget.summary()[k]
            for k in ("limit", "used_slots", "remaining_slots")
        ):
            raise ValueError("Proxy ledger mismatch")

        return execute(
            plan,
            state,
            budget,
            lambda c, v, i: trial(c, v, i, budget_override=budget),
            lambda: matrix.write_json(state_path, state),
            transport_failed,
        )


def execute(plan, state, budget, diagnostic, save, failed):
    if matrix.sha(plan) != state["plan_sha256"]:
        raise ValueError("Plan digest mismatch")
    for task in plan["tasks"]:
        entry = state["tasks"][task["id"]]
        if entry["status"] == "finished":
            continue
        if entry["status"] != "pending":
            raise ValueError("Interrupted task requires explicit reconciliation")
        if (
            budget.summary()["remaining_slots"]
            < task["protocol"]["max_provider_attempts"]
        ):
            state["status"] = "awaiting_budget"
            save()
            return state
        entry.update(status="running", dispatch_started_unix=time.time())
        state["status"] = "running"
        save()
        result = diagnostic(task["case_id"], task["variant"], task["repeat_index"])
        entry.update(status="finished", result_id=result["id"])
        if failed(result):
            state.update(
                status="awaiting_network_review", transport_failure_run=result["id"]
            )
            save()
            return state
        save()
    state["status"] = "finished"
    save()
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    state = run(args.prepare_only)
    print(
        json.dumps(
            {
                "status": state["status"],
                "finished": sum(
                    r["status"] == "finished" for r in state["tasks"].values()
                ),
                "planned": len(state["tasks"]),
            }
        )
    )
