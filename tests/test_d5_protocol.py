import ast
import copy
import json
from pathlib import Path

import pytest
from cloudsre.diagnostic_protocol import D4_SYSTEM, TOOL_SPECS
from cloudsre.diagnostic_rules import propose
from cloudsre.durable import digest
from cloudsre.experiment_views import (
    KINDS,
    aliases,
    load_records,
    resolve_citations,
    view,
)

from benchmarks.d5.catalog import DEV, VALIDATION, cases, holdout_configurations


def test_d5_control_is_exact_d4_prompt_and_tool_protocol():
    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "diagnosis/runner_v3.py").read_text()
    )
    values = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"system", "specs"}
    }
    assert D4_SYSTEM == values["system"]
    assert TOOL_SPECS == values["specs"]


def test_d5_split_sizes_and_formal_holdout_is_not_executable():
    assert len(DEV) == 12 and len(VALIDATION) == 8
    assert len({row.id for row in (*DEV, *VALIDATION)}) == 20
    assert not (
        {(r.family, r.target, r.mechanism, r.distractor, r.tool_failure) for r in DEV}
        & {
            (r.family, r.target, r.mechanism, r.distractor, r.tool_failure)
            for r in VALIDATION
        }
    )
    holdout = holdout_configurations()
    assert len(holdout) == 20
    assert all(
        row["split"] == "holdout" and row["interference_pattern"] for row in holdout
    )
    with pytest.raises(ValueError, match="disabled"):
        cases("holdout")


def test_d5_snapshot_content_hash_and_unique_ids_are_enforced(tmp_path):
    for index, kind in enumerate(KINDS):
        data = {"observed": kind}
        (tmp_path / (kind + ".json")).write_text(
            json.dumps(
                {
                    "id": "e-" + str(index),
                    "kind": kind,
                    "data": data,
                    "content_hash": digest(data),
                }
            )
        )
    loaded = load_records(tmp_path)
    assert len(loaded) == 6
    path = tmp_path / "logs.json"
    data = json.loads(path.read_text())
    data["data"]["observed"] = "tampered"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="hash"):
        load_records(tmp_path)


def test_d5_reference_resolution_is_exact_and_preserves_raw_model_output():
    raw = {"evidence_ids": ["E1", "E2"], "action": "escalate"}
    result = resolve_citations(raw, {"E1": "real-first", "E2": "real-second"})
    assert result["evidence_ids"] == ["real-first", "real-second"]
    assert raw["evidence_ids"] == ["E1", "E2"]
    for value in ("E01", "E 1", "real-first", "E3"):
        with pytest.raises(ValueError, match="fuzzy"):
            resolve_citations({"evidence_ids": [value]}, {"E1": "real-first"})


def test_d5_raw_and_compact_evidence_keep_guard_identity_and_fault_states():
    record = {
        "id": "e-identity",
        "kind": "observations",
        "data": {
            "pods": [
                {
                    "name": "order-abc",
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {
                                "name": "order",
                                "lastState": {"terminated": {"reason": "OOMKilled"}},
                                "restartCount": 2,
                            }
                        ],
                    },
                    "containers": [
                        {"name": "order", "resources": {"limits": {"memory": "8Mi"}}}
                    ],
                }
            ],
            "events": [],
            "endpoints": [],
            "event_query": {"truncated": True},
        },
    }
    original = copy.deepcopy(record)
    assert view(record, "raw") == original
    compact = view(record, "compact")
    assert compact["id"] == "e-identity"
    assert (
        compact["data"]["pods"][0]["container_states"][0]["lastState"]["terminated"][
            "reason"
        ]
        == "OOMKilled"
    )
    assert compact["data"]["event_query"]["truncated"]
    assert record == original
    records = {kind: {"id": kind} for kind in KINDS}
    assert len(set(aliases(records).values())) == 6


def test_d5_rules_do_not_use_release_recency_as_a_repair_trigger():
    def workload(name, replicas=1):
        return {
            "identity": {
                "resource": "sre-demo/Deployment/" + name,
                "spec_hash": "hash-" + name,
            },
            "spec": {
                "replicas": replicas,
                "template": {
                    "spec": {"containers": [{"name": name, "env": [], "resources": {}}]}
                },
            },
        }

    baseline = [workload(name) for name in ("gateway", "order", "inventory")]
    current = copy.deepcopy(baseline)
    current[2]["spec"]["replicas"] = 0
    records = {kind: {"id": "e-" + kind, "kind": kind, "data": {}} for kind in KINDS}
    records["resources"]["data"]["resources"] = current
    records["baseline"]["data"]["resources"] = baseline
    records["observations"]["data"] = {"pods": [], "endpoints": []}
    records["releases"]["data"] = {"newest_release": "gateway"}
    assert propose(records, "sre-demo")["resource"] == "sre-demo/Deployment/inventory"
    current[2]["spec"]["replicas"] = 1
    assert propose(records, "sre-demo")["action"] == "escalate"
