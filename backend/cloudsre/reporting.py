"""Versioned machine-readable incident report; unknown values remain null."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .call_ledger import summarize


def versions(root):
    tracked = subprocess.check_output(
        ["git", "-C", str(root), "ls-files"], text=True
    ).splitlines()
    hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in tracked
        if name.endswith((".py", ".yaml", ".yml", ".toml", ".lock"))
    }
    # Include new modules before their first commit.
    for parent in ("backend", "diagnosis"):
        for path in (root / parent).rglob("*.py"):
            hashes[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return {
        "git_commit": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "working_tree_dirty": bool(
            subprocess.check_output(
                ["git", "-C", str(root), "status", "--porcelain"], text=True
            ).strip()
        ),
        "source_hashes": hashes,
        "source_bundle_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
        "holmes_commit": (root / "docs/HOLMESGPT-COMMIT.txt").read_text().strip(),
    }


def write_report(folder, journal, software):
    folder = Path(folder)
    evidence = []
    for path in sorted((folder / "evidence").glob("*.json")):
        data = json.loads(path.read_text())
        evidence.append(
            {
                "id": data["id"],
                "kind": data["kind"],
                "source": data.get("source"),
                "collected_at": data.get("collected_at"),
                "query": data.get("data", {}).get("query"),
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    accounting = summarize(folder / "calls.jsonl")
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "incident_id": journal["incident_id"],
        "run_id": journal["run_id"],
        "outcome": journal["outcome"],
        "impact": {
            "service": "order",
            "affected_business_requests": None,
            "description": "Observed order-path alert; total affected customer requests was not measured.",
        },
        "timeline": journal["events"],
        "phase_seconds": journal.get("phase_seconds", {}),
        "hypotheses": {
            "model_assessment": journal.get("answer"),
            "proposal": journal.get("proposal"),
        },
        "evidence": evidence,
        "action": journal.get("action"),
        "verification": journal.get("quality"),
        "warmup_checks": journal.get("warmup", []),
        "unresolved": []
        if journal["outcome"] == "agent_restored_and_verified"
        else [journal.get("error", "No verified recovery")],
        "long_term_recommendations": [
            "Validate dependency URLs before rollout",
            "Require live business canary before registering a healthy release",
        ],
        "limitations": [
            "Single restore_env action and one controlled configuration incident",
            "No general recovery-rate claim; D3 restart takeover and RBAC remain pending",
            "Provider cost is unknown without a verified dated tariff",
        ],
        "versions": software,
        "model_accounting": accounting,
    }
    path = folder / "incident-report.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temp.replace(path)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "accounting": accounting,
    }
