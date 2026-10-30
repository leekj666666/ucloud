"""Freeze the protocol before opening held-out configuration contents."""

import hashlib
import json
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cloudsre.controls import Controls
from cloudsre.durable import now
from cloudsre.store import Store
from cloudsre.worker import database_url

from benchmarks.d5.catalog import Case

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "evaluation/d6-runtime"
PRIVATE = ROOT / "evaluation/d6-private"
SEALED = ROOT / "evaluation/d5-private/holdout-sealed.json"
END_TO_END = ("B0", "B2", "B3")
SMOKE = {
    "smoke_local": (
        "route",
        "inventory",
        "numeric_port",
        "two_newer_unrelated_releases",
    ),
    "smoke_cross": (
        "config",
        "order",
        "missing_dns",
        "cross_namespace_and_local_release",
    ),
}


@dataclass(frozen=True)
class FormalCase(Case):
    interference_pattern: str = "two_newer_unrelated_releases"
    distractor_targets: tuple = ()


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    files = (
        list((ROOT / "backend").rglob("*.py"))
        + list((ROOT / "diagnosis").rglob("*.py"))
        + list((ROOT / "benchmarks").rglob("*.py"))
    )
    files += list((ROOT / "scripts").glob("*.py"))
    files += [
        ROOT / "uv.lock",
        ROOT / "frontend/package-lock.json",
        ROOT / "docs/HOLMESGPT-COMMIT.txt",
    ]
    return {str(p.relative_to(ROOT)): checksum(p) for p in sorted(files)}


def create_schedule(seed):
    tasks = [
        {"case_id": f"holdout_{i:02d}", "variant": v, "repeat": repeat}
        for i in range(1, 21)
        for repeat in range(3)
        for v in END_TO_END
    ]
    random.Random(seed).shuffle(tasks)
    return [
        {**t, "id": f"{t['case_id']}__{t['variant']}__{t['repeat']}"} for t in tasks
    ]


def freeze(image_id, model_name, seed=20261031):
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PRIVATE.chmod(0o700)
    plan = {
        "schema_version": 1,
        "created_at": now(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_hashes": source_hashes(),
        "sealed_sha256": checksum(SEALED),
        "image_id": image_id,
        "model": model_name,
        "default_H2b": False,
        "default_H5": False,
        "thinking": "original provider default enabled",
        "temperature": 0,
        "top_p": 1,
        "max_output_tokens": 8192,
        "max_provider_attempts": 4,
        "automatic_provider_retries": 0,
        "seed": seed,
        "tasks": create_schedule(seed),
        "B1_diagnostic_replays": 60,
        "metrics": {
            "recovery_all": "all scheduled cases, including unsupported",
            "recovery_eligible": "scheduled cases with expected_disposition recover",
            "correct_disposition": "all scheduled cases; escalation never counts as recovery",
            "uncertainty_unit": "case configuration, not each correlated repetition",
            "cost_unknown": "retain unknown usage and amount",
        },
        "quality": {"seconds": 120, "requests": 120, "business_checks": 12},
        "semantics": "held-out compound release contexts within known fault families, not third-party blind testing",
        "execution": "same sandbox bounded executor for all groups; production manual approval validated separately",
        "environment_failures": "preserve original; stop; reconcile; explicitly link a new attempt without replacing result",
        "agent_failures": "invalid output, policy refusal, model timeout and wrong plan remain outcomes, never environment exclusions",
    }
    path = PRIVATE / "protocol.json"
    with path.open("x") as stream:
        json.dump(plan, stream, indent=2)
    path.chmod(0o600)
    return plan


def verify_protocol():
    plan = json.loads((PRIVATE / "protocol.json").read_text())
    if plan["source_hashes"] != source_hashes() or plan["sealed_sha256"] != checksum(
        SEALED
    ):
        raise ValueError("Frozen D6 source or sealed configurations changed")
    return plan


def open_holdout():
    plan = verify_protocol()
    rows = json.loads(SEALED.read_text())["cases"]
    if len(rows) != 20 or {x["id"] for x in rows} != {
        f"holdout_{i:02d}" for i in range(1, 21)
    }:
        raise ValueError("Unexpected sealed configuration identities")
    marker = PRIVATE / "opened.json"
    if not marker.exists():
        marker.write_text(
            json.dumps(
                {
                    "opened_at": now(),
                    "protocol_sha256": checksum(PRIVATE / "protocol.json"),
                    "sealed_sha256": plan["sealed_sha256"],
                }
            )
        )
        marker.chmod(0o600)
    return rows


def case_by_id(ident):
    if ident in SMOKE:
        family, target, mechanism, pattern = SMOKE[ident]
        others = tuple(sorted({"gateway", "order", "inventory"} - {target}))
        return FormalCase(
            ident,
            "dev",
            family,
            target,
            mechanism,
            others[0],
            False,
            "recover",
            pattern,
            others,
        )
    verify_protocol()
    if not (PRIVATE / "opened.json").is_file():
        raise ValueError(
            "Formal holdout must be explicitly opened after protocol freeze"
        )
    rows = json.loads(SEALED.read_text())["cases"]
    row = next((r for r in rows if r["id"] == ident), None)
    if row is None:
        raise ValueError("Unknown formal case")
    others = tuple(row["distractor_targets"])
    return FormalCase(
        row["id"],
        "holdout",
        row["family"],
        row["target"],
        row["mechanism"],
        others[0],
        False,
        "escalate" if row["family"] == "unsupported" else "recover",
        row["interference_pattern"],
        others,
    )


def prepare_environment():
    store = Store(database_url())
    try:
        if Controls(store).pending():
            raise RuntimeError(
                "Active production-controlled incident prevents D6 injection"
            )
    finally:
        store.engine.dispose()
    subprocess.run(
        ["docker", "stop", "cloudsre-worker-d4-1", "cloudsre-worker-d4-2"],
        check=True,
        capture_output=True,
    )
    for p in (PRIVATE / "captures").rglob("fixture.json"):
        if json.loads(p.read_text()).get("active"):
            raise RuntimeError("An interrupted D6 fixture requires explicit recovery")
