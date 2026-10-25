"""Serial real-observation capture; formal holdout is sealed and never executed."""

import argparse
import hashlib
import json
import subprocess
import threading
import uuid

from cloudsre.controls import Controls
from cloudsre.store import Store
from cloudsre.worker import database_url

from .catalog import DEV, VALIDATION, cases, holdout_configurations
from .live import PRIVATE, ROOT, WORK, Fixture, capture, configure, exclusive


def prepare():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    sealed = PRIVATE / "holdout-sealed.json"
    if not sealed.exists():
        sealed.write_text(
            json.dumps(
                {
                    "split": "holdout",
                    "cases": holdout_configurations(),
                    "randomization_seed": uuid.uuid4().hex,
                },
                indent=2,
            )
        )
        sealed.chmod(0o600)
    public = {
        "counts_semantics": "Configured definitions; actual scoring-ready captures are reported separately",
        "development_cases": len(DEV),
        "validation_cases": len(VALIDATION),
        "sealed_holdout_cases": 20,
        "holdout_sha256": hashlib.sha256(sealed.read_bytes()).hexdigest(),
        "holdout_not_executed_in_D5": True,
        "case_independence": "Configurations/causal context within known fault families; not 40 independent production failure classes",
    }
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "split-manifest.json").write_text(json.dumps(public, indent=2))
    store = Store(database_url())
    pending = Controls(store).pending()
    store.engine.dispose()
    if pending:
        raise RuntimeError(
            "An operator-controlled task is active; do not inject D5 faults"
        )
    subprocess.run(
        ["docker", "stop", "cloudsre-worker-d4-1", "cloudsre-worker-d4-2"],
        check=True,
        capture_output=True,
    )
    registry = {row.id: row for row in (*DEV, *VALIDATION)}
    paths = list((PRIVATE / "captures").glob("*/fixture.json")) + list(
        (ROOT / "evaluation/runs").glob("d5-controls-*/fixture/fixture.json")
    )
    for path in paths:
        record = json.loads(path.read_text())
        if not record.get("active"):
            continue
        case = registry.get(record.get("case", {}).get("id"))
        if not case or case.public_description() != record["case"]:
            raise ValueError(
                "Interrupted fixture version is unknown; automatic cleanup refused"
            )
        fixture = Fixture.__new__(Fixture)
        fixture.case = case
        fixture.folder = path.parent
        fixture.path = path
        fixture.record = record
        fixture.stop = threading.Event()
        fixture.traffic = None
        restored = fixture.cleanup()
        (path.parent / "interruption-recovery.json").write_text(
            json.dumps(
                {
                    "actor": "evaluator owned-field recovery",
                    "restored_fields": restored,
                    "no_agent_recovery_claim": True,
                },
                indent=2,
            )
        )
    return public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "validation"], required=True)
    parser.add_argument("--case")
    args = parser.parse_args()
    configure()
    with exclusive():
        prepare()
        index_path = PRIVATE / "capture-index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        for case in cases(args.split):
            if args.case and case.id != args.case:
                continue
            if (
                index.get(case.id, {}).get("passed")
                and index[case.id].get("scoring_ready")
                and (
                    not case.distractor
                    or index[case.id].get("distractor_after_fault") is True
                )
            ):
                continue
            run = "capture-" + case.id + "-" + uuid.uuid4().hex[:8]
            folder = PRIVATE / "captures" / run
            print("Capturing " + case.id, flush=True)
            result = capture(case, folder)
            if case.id in index:
                history = PRIVATE / "capture-revisions.jsonl"
                with history.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "case_id": case.id,
                                "previous": index[case.id],
                                "replacement": result,
                            }
                        )
                        + "\n"
                    )
            index[case.id] = result
            index_path.write_text(json.dumps(index, indent=2))
            print(
                json.dumps(
                    {
                        "case": case.id,
                        "snapshot": result["snapshot_id"],
                        "passed": result["passed"],
                    }
                ),
                flush=True,
            )
        (WORK / "capture-progress.json").write_text(
            json.dumps(
                {
                    "captured_dev": sum(
                        value.get("split") == "dev"
                        and value.get("passed")
                        and value.get("scoring_ready")
                        for value in index.values()
                    ),
                    "captured_validation": sum(
                        value.get("split") == "validation"
                        and value.get("passed")
                        and value.get("scoring_ready")
                        for value in index.values()
                    ),
                    "paid_model_calls": 0,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
