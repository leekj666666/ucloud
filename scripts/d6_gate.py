"""Preformal smoke proof and protocol-freeze entry points; no implicit unseal."""

import argparse
import json
import subprocess

import httpx
from dotenv import dotenv_values

from benchmarks.d6 import diagnose
from benchmarks.d6.protocol import PRIVATE, ROOT, WORK, freeze, open_holdout

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["smoke-proof", "model-smoke", "freeze", "open"])
args = parser.parse_args()
if args.command == "smoke-proof":
    rows = []
    for ident in ("smoke_local", "smoke_cross"):
        found = [
            json.loads(p.read_text()) for p in (WORK / "e2e").glob("*/result.json")
        ]
        selected = [r for r in found if r["case_id"] == ident and r["variant"] == "B0"]
        assert len(selected) == 1, (
            "Unexpected smoke denominator; review instead of selecting best result"
        )
        r = selected[0]
        assert (
            r["completed"]
            and r["external_recovery"]
            and r["quality"]["passed"]
            and r["cleanup"]["passed"]
        )
        private = PRIVATE / "captures" / ("e2e-" + r["id"])
        fixture = json.loads((private / "fixture.json").read_text())
        assert not fixture["active"]
        if ident == "smoke_cross":
            assert not json.loads((private / "peer/fixture.json").read_text())["active"]
        rows.append(
            {
                "id": r["id"],
                "case": ident,
                "passed": True,
                "model_calls": r["provider_http_attempts"],
            }
        )
    other = httpx.post("http://localhost:18086/verify", timeout=40).json()
    assert other["passed"] and other["cleanup"]["cleaned"]
    proof = {
        "passed": True,
        "rows": rows,
        "peer_business_passed": True,
        "holdout_opened": (PRIVATE / "opened.json").exists(),
    }
    (WORK / "preformal-smoke.json").write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof))
elif args.command == "model-smoke":
    from cloudsre.model_budget import ModelBudget

    smoke = json.loads((WORK / "preformal-smoke.json").read_text())
    episode = smoke["rows"][0]["id"]
    capture = json.loads(
        (PRIVATE / "captures" / ("e2e-" + episode) / "capture.json").read_text()
    )
    budget = ModelBudget(
        ROOT / "evaluation/d5-runtime/budget-private/batch4.sqlite", 100
    )
    health = httpx.get("http://localhost:18090/health", timeout=10).json()
    assert all(
        health.get(k) == budget.summary()[k]
        for k in ("limit", "used_slots", "remaining_slots")
    )
    assert not (WORK / "model-smoke.json").exists(), (
        "Do not automatically retry preformal model call"
    )
    result = diagnose.trial(
        "smoke_local", "B3", 0, capture_override=capture, budget_override=budget
    )
    proof = {
        "result_id": result["id"],
        "completed": result["completed"],
        "output_valid": result["output_valid"],
        "calls": result["provider_http_attempts"],
        "formal_holdout_used": False,
    }
    (WORK / "model-smoke.json").write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof))
elif args.command == "freeze":
    assert json.loads((WORK / "preformal-smoke.json").read_text())["passed"]
    assert json.loads((WORK / "regression/summary.json").read_text())["passed"]
    proof = json.loads((WORK / "model-smoke.json").read_text())
    assert proof["completed"] and proof["calls"] > 0
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True
    ).strip(), "Commit tested version before freezing"
    from benchmarks.d6.protocol import SEALED, checksum

    original = json.loads(
        (ROOT / "evaluation/d5-runtime/split-manifest.json").read_text()
    )
    assert original["holdout_sha256"] == checksum(SEALED), (
        "Original sealed hash differs"
    )
    worker = dotenv_values(ROOT / "infra/.generated/d4/worker.env")
    assert worker.get("CLOUDSRE_H2B_ENABLED", "0") != "1"
    image = diagnose.image_provenance()
    model = dotenv_values("/mnt/d/uclaude/model.env")["MODEL_NAME"]
    plan = freeze(
        image["image_id"],
        model,
        baseline_sha256=checksum(ROOT / "infra/.generated/d4/baseline.json"),
    )
    print(
        json.dumps(
            {"frozen": True, "tasks": len(plan["tasks"]), "holdout_opened": False}
        )
    )
else:
    rows = open_holdout()
    print(json.dumps({"opened_after_freeze": True, "case_count": len(rows)}))
