"""Core-environment preparation/validation, separate from Holmes dependencies."""

import json
import os
import sys
from pathlib import Path

from .diagnostic_rules import propose
from .experiment_variants import settings
from .experiment_views import KINDS, aliases, load_records, resolve_citations, view
from .policy_v3 import parse


def main():
    stage, folder, variant = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    if stage == "prepare":
        records = load_records(os.environ["CLOUDSRE_SNAPSHOT_DIR"])
        incident = json.loads((folder / "incident.json").read_text())
        protocol = settings(variant)
        mode = protocol["view"]
        mapping = aliases(records)
        supplied = {
            kind: view(
                records[kind],
                mode,
                next(
                    key
                    for key, value in mapping.items()
                    if value == records[kind]["id"]
                ),
            )
            for kind in KINDS
        }
        rule = (
            propose(records, incident["namespace"])
            if variant in {"B0", "H5_hybrid"}
            else None
        )
        if protocol["single_call"]:
            (folder / "evidence").mkdir(exist_ok=True)
            for record in records.values():
                (folder / "evidence" / (record["id"] + ".json")).write_text(
                    json.dumps(record, ensure_ascii=False)
                )
        (folder / "prepared-input.json").write_text(
            json.dumps(
                {
                    "mode": mode,
                    "mapping": mapping,
                    "observations": supplied,
                    "rule": rule,
                },
                ensure_ascii=False,
            )
        )
    elif stage == "finalize":
        proposal = parse((folder / "proposal-raw.txt").read_text()).model_dump()
        if settings(variant)["view"] == "compact_alias":
            mapping = json.loads((folder / "prepared-input.json").read_text())[
                "mapping"
            ]
            proposal = resolve_citations(proposal, mapping)
            (folder / "reference-map.json").write_text(json.dumps(mapping))
        (folder / "proposal.json").write_text(
            json.dumps(proposal, ensure_ascii=False, indent=2)
        )
    else:
        raise ValueError("Unknown core diagnostic stage")


if __name__ == "__main__":
    main()
