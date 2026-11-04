"""Production read-only snapshot and deterministic routing, never execution."""

import json
import sys
import time
import uuid
from pathlib import Path

import httpx

from .diagnostic_rules import propose
from .durable import digest, now
from .evidence_v3 import collect
from .experiment_views import KINDS


def prepare(folder, mode):
    folder = Path(folder)
    if mode not in {"h5", "b3"}:
        raise ValueError("Unsupported routing mode")
    target = folder / "snapshot"
    target.mkdir(exist_ok=False)
    records = {}
    for kind in KINDS:
        try:
            data = collect(kind, folder)
        except (httpx.HTTPError, RuntimeError) as exc:
            data = {
                "collection_error": {
                    "error_type": type(exc).__name__,
                    "source": "read-only collection",
                }
            }
        record = {
            "id": "e-" + uuid.uuid4().hex,
            "kind": kind,
            "collected_at": now(),
            "source": "live-read-only-tool",
            "data": data,
            "content_hash": digest(data),
        }
        records[kind] = record
        (target / (kind + ".json")).write_text(json.dumps(record, ensure_ascii=False))
    # Log shipping is asynchronous. Bounded reads retain every actual response;
    # never extend the incident window backwards or synthesize missing fields.
    log_attempts = folder / "log-collection-attempts"
    log_attempts.mkdir()
    (log_attempts / "0.json").write_text(json.dumps(records["logs"]))
    if mode == "h5" and not records["logs"]["data"].get("collection_error"):
        deadline = time.monotonic() + 18
        for attempt in range(1, 7):
            if records["logs"]["data"].get("lines") or time.monotonic() >= deadline:
                break
            time.sleep(3)
            try:
                data = collect("logs", folder)
            except (httpx.HTTPError, RuntimeError) as exc:
                data = {
                    "collection_error": {
                        "error_type": type(exc).__name__,
                        "source": "bounded log supplement",
                    }
                }
            record = {
                "id": "e-" + uuid.uuid4().hex,
                "kind": "logs",
                "collected_at": now(),
                "source": "live-read-only-tool",
                "data": data,
                "content_hash": digest(data),
            }
            (log_attempts / (str(attempt) + ".json")).write_text(json.dumps(record))
            records["logs"] = record
            (target / "logs.json").write_text(json.dumps(record))
            if data.get("collection_error"):
                break
    incident = json.loads((folder / "incident.json").read_text())
    rule = None
    errors = [kind for kind in KINDS if records[kind]["data"].get("collection_error")]
    if mode == "h5" and not errors:
        rule = propose(records, incident["namespace"])
    selected = rule if rule and rule["action"] != "escalate" else None
    receipt = {
        "mode": mode,
        "route": "rule" if selected else "model",
        "collection_errors": errors,
        "snapshot_hashes": {k: digest(v) for k, v in records.items()},
        "execution_authorized": False,
        "approval_required": True,
    }
    (folder / "routing.json").write_text(json.dumps(receipt, indent=2))
    if selected:
        evidence = folder / "evidence"
        evidence.mkdir(exist_ok=True)
        for record in records.values():
            (evidence / (record["id"] + ".json")).write_text(
                json.dumps(record, ensure_ascii=False)
            )
        (folder / "proposal.json").write_text(json.dumps(selected, ensure_ascii=False))
    return receipt


if __name__ == "__main__":
    prepare(sys.argv[1], sys.argv[2])
