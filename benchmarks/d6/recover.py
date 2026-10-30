"""Explicit evaluator-only owned-field recovery, never an Agent success."""

import argparse
import json
import subprocess
import threading

from cloudsre.durable import digest, now

from benchmarks.d5 import live

from .fixture import Fixture
from .protocol import PRIVATE, case_by_id


def restore(folder):
    folder = folder.resolve()
    if folder.parent != (PRIVATE / "captures").resolve() or not folder.name.startswith(
        "e2e-"
    ):
        raise ValueError("Recovery outside exact D6 episode fixture refused")
    if subprocess.check_output(
        ["docker", "ps", "--filter", "name=cloudsre-d5-diag-", "-q"]
    ).strip():
        raise ValueError("A diagnostic process is still active")
    record = json.loads((folder / "fixture.json").read_text())
    case = case_by_id(record["case"]["id"])
    if digest(record["case"]) != digest(case.public_description()):
        raise ValueError("Fixture definition changed")
    f = Fixture.__new__(Fixture)
    f.case = case
    f.folder = folder
    f.path = folder / "fixture.json"
    f.record = record
    f.stop = threading.Event()
    f.traffic = None
    f.peer = None
    peer = record.get("peer_fixture_folder")
    if peer:
        from pathlib import Path

        if Path(peer).resolve() != folder / "peer":
            raise ValueError("Unexpected peer fixture path")
        f.peer = live.Fixture.__new__(live.Fixture)
        f.peer.case = case
        f.peer.folder = folder / "peer"
        f.peer.path = f.peer.folder / "fixture.json"
        f.peer.record = json.loads(f.peer.path.read_text())
        f.peer.stop = threading.Event()
        f.peer.traffic = None
    fields = f.cleanup() if record["active"] else []
    proof = {
        "actor": "explicit evaluator recovery",
        "at": now(),
        "restored_fields": fields,
        "no_agent_recovery_claim": True,
    }
    (folder / "interruption-recovery.json").write_text(json.dumps(proof, indent=2))
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_id")
    args = parser.parse_args()
    if len(args.episode_id) != 32 or any(
        c not in "0123456789abcdef" for c in args.episode_id
    ):
        raise ValueError("Invalid episode ID")
    live.configure()
    with live.exclusive():
        print(json.dumps(restore(PRIVATE / "captures" / ("e2e-" + args.episode_id))))
