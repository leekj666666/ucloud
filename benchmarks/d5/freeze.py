"""Freeze complete real captures before validation; never read formal holdout."""

import json
from pathlib import Path
from types import SimpleNamespace

from cloudsre.durable import digest, now
from cloudsre.experiment_views import load_records

from .catalog import DEV, VALIDATION
from .live import PRIVATE, WORK, confirm_release_order


def freeze():
    index_path = PRIVATE / "capture-index.json"
    index = json.loads(index_path.read_text())
    frozen = {}
    for case in (*DEV, *VALIDATION):
        capture = index[case.id]
        if not capture.get("passed") or not capture.get("scoring_ready"):
            raise ValueError("Missing real scoring-ready capture: " + case.id)
        if case.distractor and capture.get("distractor_after_fault") is not True:
            raise ValueError(
                "H1 requires an unrelated release newer than the fault: " + case.id
            )
        snapshot = WORK / "snapshots" / capture["snapshot_id"]
        records = load_records(snapshot)
        if case.distractor:
            fixture = json.loads(
                (Path(capture["capture_folder"]) / "fixture.json").read_text()
            )
            confirm_release_order(
                case, SimpleNamespace(record=fixture, persist=lambda: None), records
            )
        hashes = {kind: digest(record) for kind, record in records.items()}
        if hashes != capture["records_sha256"]:
            raise ValueError("Capture-time complete record hash differs: " + case.id)
        incident_hash = digest(json.loads((snapshot / "incident.json").read_text()))
        if capture.get("incident_sha256", incident_hash) != incident_hash:
            raise ValueError("Capture-time incident differs: " + case.id)
        # Earlier developer captures predate the incident-hash addition. Explicitly
        # record its first freeze, rather than mislabel it a capture-time checksum.
        if "incident_sha256" not in capture:
            capture["incident_sha256"] = incident_hash
            capture["incident_hash_first_frozen_at"] = now()
            capture["incident_hash_frozen_after_capture"] = True
        frozen[case.id] = {
            "snapshot_id": capture["snapshot_id"],
            "records_sha256": hashes,
            "incident_sha256": incident_hash,
        }
    path = PRIVATE / "frozen-snapshots.json"
    if path.exists():
        if json.loads(path.read_text())["captures"] != frozen:
            raise ValueError("Refusing to change an already frozen experiment dataset")
    else:
        path.write_text(json.dumps({"frozen_at": now(), "captures": frozen}, indent=2))
        path.chmod(0o600)
    index_path.write_text(json.dumps(index, indent=2))
    print(
        json.dumps({"frozen_captures": len(frozen), "manifest_sha256": digest(frozen)})
    )


if __name__ == "__main__":
    freeze()
