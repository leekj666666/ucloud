"""Read-only benchmark observations. No evaluator labels, injectors or answers."""

import copy
import json
import os
import sys
from pathlib import Path

from .durable import digest, now
from .evidence_v3 import model_view

KINDS = ("resources", "observations", "baseline", "releases", "logs", "metrics")


def load_records(folder):
    folder = Path(folder)
    records = {
        kind: json.loads((folder / (kind + ".json")).read_text()) for kind in KINDS
    }
    for kind, record in records.items():
        if record["kind"] != kind or record["content_hash"] != digest(record["data"]):
            raise ValueError("Snapshot identity or content hash mismatch")
    if len({record["id"] for record in records.values()}) != len(KINDS):
        raise ValueError("Snapshot evidence IDs must be unique")
    return records


def aliases(records):
    return {
        "E" + str(index + 1): records[kind]["id"] for index, kind in enumerate(KINDS)
    }


def view(record, mode="compact", reference=None):
    if mode not in {"raw", "compact", "compact_alias"}:
        raise ValueError("Unknown evidence view")
    if record["data"].get("collection_error"):
        result = copy.deepcopy(record)
    else:
        result = copy.deepcopy(record) if mode == "raw" else model_view(record)
    if mode == "compact_alias":
        if reference is None:
            raise ValueError("A trusted exact reference is required")
        result["id"] = reference
        result["reference_semantics"] = (
            "Exact run-local reference; resolves only against this observation snapshot."
        )
    return result


def resolve_citations(proposal, mapping):
    result = copy.deepcopy(proposal)
    if any(value not in mapping for value in result["evidence_ids"]):
        raise ValueError(
            "Unknown exact evidence reference; fuzzy correction is forbidden"
        )
    result["evidence_ids"] = [mapping[value] for value in result["evidence_ids"]]
    return result


def main():
    if os.environ.get("CLOUDSRE_BENCHMARK_READ_ONLY") != "1":
        raise ValueError(
            "Benchmark evidence access is disabled outside its isolated runner"
        )
    kind = sys.argv[1]
    if kind not in KINDS:
        raise ValueError("Unsupported fixed read-only tool")
    folder = Path(os.environ["CLOUDSRE_RUN_DIR"])
    records = load_records(Path(os.environ["CLOUDSRE_SNAPSHOT_DIR"]))
    record = records[kind]
    mapping = aliases(records)
    reference = next(key for key, value in mapping.items() if value == record["id"])
    output = view(
        record, os.environ.get("CLOUDSRE_EVIDENCE_VIEW", "compact"), reference
    )
    (folder / "evidence").mkdir(exist_ok=True)
    (folder / "evidence" / (record["id"] + ".json")).write_text(
        json.dumps(record, ensure_ascii=False)
    )
    with (folder / "tool-calls.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "at": now(),
                    "tool": kind,
                    "arguments": {},
                    "evidence_id": record["id"],
                    "content_hash": record["content_hash"],
                    "view_bytes": len(json.dumps(output, ensure_ascii=False).encode()),
                }
            )
            + "\n"
        )
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
