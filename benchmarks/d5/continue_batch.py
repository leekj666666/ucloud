"""Prepared continuation adapter. Requires a separate explicit user authorization.

This module neither creates approval nor deploys a proxy. Existing frozen
protocol files are accepted only with the two byte-exact host-runner edits.
"""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cloudsre.experiment_variants import settings
from cloudsre.model_budget import ModelBudget

from scripts.archive_d5_evidence import public_budget

from . import matrix
from .live import PRIVATE, ROOT, WORK, exclusive

FROZEN_COMMIT = "9ad05e5c8013193d786fbf49c20cb8eebcd417b1"
RUNNER = "benchmarks/d5/diagnose.py"
OLD_SIGNATURE = "def trial(case_id, variant, repeat=0, *, capture_override=None):"
NEW_SIGNATURE = "def trial(case_id, variant, repeat=0, *, capture_override=None, budget_override=None):"
OLD_BUDGET = '    budget = ModelBudget(WORK / "budget-private/batch.sqlite", 100)'
NEW_BUDGET = '    budget = (\n        budget_override\n        if budget_override is not None\n        else ModelBudget(WORK / "budget-private/batch.sqlite", 100)\n    )'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def authorization(value):
    if not isinstance(value, dict):
        raise TypeError("Authorization must be a JSON object")
    if (
        value.get("batch_id") != "b2"
        or value.get("user_approved") is not True
        or type(value.get("limit")) is not int
        or not 1 <= value["limit"] <= 100
        or not isinstance(value.get("source_reply"), str)
        or not value["source_reply"].strip()
        or not isinstance(value.get("approved_at"), str)
    ):
        raise ValueError("Missing or invalid explicit batch-two user authorization")
    approved = datetime.fromisoformat(value["approved_at"])
    if approved.tzinfo is None or approved > datetime.now(UTC):
        raise ValueError("Approval requires an existing timezone-aware timestamp")
    global_limit = value.get("global_total_limit")
    if global_limit is not None and (
        type(global_limit) is not int or not 100 + value["limit"] <= global_limit <= 400
    ):
        raise ValueError(
            "Active first/second batch limits exceed the explicitly authorized cumulative ceiling"
        )
    return value


def adapter(original):
    text = original.decode()
    if text.count(OLD_SIGNATURE) != 1 or text.count(OLD_BUDGET) != 1:
        raise ValueError(
            "Frozen runner does not contain exactly the two expected adapter sites"
        )
    return (
        text.replace(OLD_SIGNATURE, NEW_SIGNATURE)
        .replace(OLD_BUDGET, NEW_BUDGET)
        .encode()
    )


def verify_sources(expected, actual, original, current):
    if sha(original) != expected.get(RUNNER) or current != adapter(original):
        raise ValueError("Host runner differs from the byte-exact two-site adapter")
    allowed = {**expected, RUNNER: sha(current)}
    if actual != allowed:
        raise ValueError("A frozen protocol, baseline or other source changed")
    return {
        "frozen_commit": FROZEN_COMMIT,
        "old_runner_sha256": sha(original),
        "new_runner_sha256": sha(current),
        "all_other_frozen_source_hashes_unchanged": True,
    }


def verify_frozen(plan, captures):
    for task in plan["tasks"]:
        if (
            captures[task["case_id"]] != task["snapshot"]
            or settings(task["variant"]) != task["protocol"]
        ):
            raise ValueError(
                "Frozen observation or generation/per-run protocol changed"
            )


def no_inflight(summary):
    if summary.get("available") is False or any(
        row.get("state") != "finished" for row in summary.get("attempts", [])
    ):
        raise ValueError(
            "A ledger is absent or contains an uncertain in-flight attempt"
        )


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def resume(
    plan,
    state,
    *,
    approved,
    budget,
    health,
    first,
    trial_fn,
    save_fn,
    results_fn,
    sidecar_fn,
):
    """Testable gate and scheduler; finished tasks are never called again."""
    authorization(approved)
    summary = budget.summary()
    no_inflight(first)
    no_inflight(summary)
    if first.get("limit") != 100 or summary["limit"] != approved["limit"]:
        raise ValueError("Budget limit does not match its authorized batch")
    if any(
        health.get(key) != summary.get(key)
        for key in ("limit", "used_slots", "remaining_slots")
    ):
        raise ValueError("Active proxy does not match the separate second batch")
    if any(
        row.get("status") not in {"finished", "pending"}
        for row in state["tasks"].values()
    ):
        raise ValueError(
            "Interrupted/running tasks require manual reconciliation, never automatic rerun"
        )

    if set(state["tasks"]) != {task["id"] for task in plan["tasks"]}:
        raise ValueError("Checkpoint task identities differ from the original schedule")
    existing = {row["id"] for row in results_fn()}
    if any(
        row.get("result_id") not in existing
        for row in state["tasks"].values()
        if row["status"] == "finished"
    ):
        raise ValueError("An original finished task has lost its preserved result")

    def continued(case, variant, repeat):
        no_inflight(budget.summary())
        result = trial_fn(case, variant, repeat, budget_override=budget)
        sidecar_fn(
            {
                "batch_id": "b2",
                "run_id": result["id"],
                "case_id": case,
                "variant": variant,
                "repeat_index": repeat,
                "plan_sha256": state["plan_sha256"],
                "provider_http_attempts": result.get("provider_http_attempts"),
            }
        )
        return result

    return matrix.execute(
        plan,
        state,
        budget=budget,
        trial_fn=continued,
        save_fn=save_fn,
        results_fn=results_fn,
    )


def run(
    approval_file,
    *,
    directory=WORK / "matrix",
    proxy_health="http://localhost:18090/health",
):
    # Validate before ModelBudget construction or creating continuation evidence.
    approved = authorization(json.loads(Path(approval_file).read_text()))
    with exclusive():
        plan = json.loads((directory / "plan.json").read_text())
        state_path = directory / "state.json"
        state = json.loads(state_path.read_text())
        if matrix.sha(plan) != state["plan_sha256"]:
            raise ValueError("Original frozen schedule changed")
        original = subprocess.check_output(
            ["git", "show", FROZEN_COMMIT + ":" + RUNNER], cwd=ROOT
        )
        proof = verify_sources(
            plan["source_hashes"],
            matrix.protocol_hashes(),
            original,
            (ROOT / RUNNER).read_bytes(),
        )
        captures = matrix.frozen_captures(
            json.loads((PRIVATE / "capture-index.json").read_text())
        )
        verify_frozen(plan, captures)
        first_path, second_path = (
            WORK / "budget-private/batch.sqlite",
            WORK / "budget-private/batch2.sqlite",
        )
        first = public_budget(first_path)
        no_inflight(first)
        if not second_path.is_file():
            raise ValueError(
                "Authorized second-batch proxy/database must already be configured by the operator"
            )
        second = public_budget(second_path)
        no_inflight(second)
        response = httpx.get(proxy_health, timeout=10)
        response.raise_for_status()
        health = response.json()
        if second["limit"] != approved["limit"] or any(
            health.get(key) != second.get(key)
            for key in ("limit", "used_slots", "remaining_slots")
        ):
            raise ValueError(
                "Separate ledger and active proxy do not match the approval"
            )
        folder = WORK / "batch2-continuation"
        checkpoint = folder / "initial-proof.json"
        identity = {
            "authorization": approved,
            "plan_sha256": matrix.sha(plan),
            "adapter": proof,
        }
        if checkpoint.exists():
            previous = json.loads(checkpoint.read_text())
            if any(previous[key] != value for key, value in identity.items()):
                raise ValueError("Existing continuation proof differs")
        else:
            write_new(
                checkpoint,
                {
                    **identity,
                    "created_at": datetime.now(UTC).isoformat(),
                    "prior_matrix_state": state,
                    "batch1_before": first,
                    "batch2_before": second,
                },
            )
        from .diagnose import trial

        try:
            result = resume(
                plan,
                state,
                approved=approved,
                budget=ModelBudget(second_path, approved["limit"]),
                health=health,
                first=first,
                trial_fn=trial,
                save_fn=lambda value: matrix.write_json(state_path, value),
                results_fn=lambda: [
                    json.loads(path.read_text())
                    for path in (WORK / "results").glob("*.json")
                ],
                sidecar_fn=lambda value: write_new(
                    folder / "runs" / (value["run_id"] + ".json"), value
                ),
            )
        finally:
            after = public_budget(first_path)
            ledgers = {
                "authorization": approved,
                "batch1_after": after,
                "batch2_after": public_budget(second_path),
                "batch1_unchanged": after == first,
                "matrix_status": state["status"],
                "plan_sha256": matrix.sha(plan),
            }
            ledgers.update(
                global_total_limit=approved.get("global_total_limit"),
                active_batch_limit_total=after["limit"]
                + ledgers["batch2_after"]["limit"],
                cumulative_used_slots=after["used_slots"]
                + ledgers["batch2_after"]["used_slots"],
            )
            write_new(
                folder
                / (
                    "ledgers-"
                    + datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                    + ".json"
                ),
                ledgers,
            )
        if after != first:
            raise ValueError(
                "First-batch ledger changed during continuation; inspect before further runs"
            )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--directory", type=Path, default=WORK / "matrix")
    parser.add_argument("--proxy-health", default="http://localhost:18090/health")
    args = parser.parse_args()
    state = run(
        args.authorization, directory=args.directory, proxy_health=args.proxy_health
    )
    print(
        json.dumps(
            {
                "batch_id": "b2",
                "matrix_status": state["status"],
                "finished_tasks": sum(
                    row["status"] == "finished" for row in state["tasks"].values()
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
