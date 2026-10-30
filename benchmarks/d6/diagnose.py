"""Fixed diagnostic replay variants; no mutation or recovery claim."""

import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

from cloudsre.durable import digest
from cloudsre.experiment_variants import VARIANTS, settings
from cloudsre.model_budget import ModelBudget
from cloudsre.policy_v3 import authorize, parse
from dotenv import dotenv_values

from .protocol import ROOT, WORK, case_by_id


def image_provenance():
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "cloudsre-agent:d6", "--format", "{{.Id}}"],
        text=True,
    ).strip()
    embedded = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "cat",
                image_id,
                "/app/d5-build-manifest.json",
            ],
            text=True,
        )
    )
    expected = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT / "backend/cloudsre").rglob("*.py")
    }
    runner = ROOT / "diagnosis/runner_d5.py"
    expected[str(runner.relative_to(ROOT))] = hashlib.sha256(
        runner.read_bytes()
    ).hexdigest()
    if any(
        embedded["source_hashes"].get(name) != checksum
        for name, checksum in expected.items()
    ):
        raise ValueError(
            "Diagnostic image differs from the current frozen Agent sources"
        )
    return {"image_id": image_id, "embedded_build_manifest": embedded}


def score(case, capture, proposal, records):
    result = {
        "recovery_measured": False,
        "diagnostic_replay_only": True,
        "textual_root_cause_accuracy_measured": False,
    }
    fixture = json.loads((Path(capture["capture_folder"]) / "fixture.json").read_text())
    change = fixture["changes"][fixture.get("root_change_index", -1)]
    action = {
        "config": "restore_env",
        "route": "restore_service_port",
        "dependency": "restore_replicas",
        "resources": "restore_resources",
    }.get(case.family)
    resource = (
        "sre-demo/"
        + ("Service" if change["kind"] == "service" else "Deployment")
        + "/"
        + case.target
    )
    result["expected_disposition"] = case.expected_disposition
    result["root_action_proxy_correct"] = (
        bool(
            proposal
            and proposal["action"] == action
            and proposal["resource"] == resource
            and proposal["value"] == change["original"]
        )
        if action
        else None
    )
    result["unsupported_task"] = action is None
    if proposal is None:
        return {
            **result,
            "proposal_valid": False,
            "policy_authorized": False,
            "failure_category": "invalid_or_missing_model_output",
        }
    result["proposal_valid"] = True
    if proposal["action"] == "escalate":
        return {
            **result,
            "policy_authorized": False,
            "disposition": "escalated",
            "disposition_correct": case.expected_disposition == "escalate",
            "failure_category": None
            if case.expected_disposition == "escalate"
            else "model_escalated_recoverable_task",
        }
    try:
        raw_current = json.loads(
            (Path(capture["capture_folder"]) / "current-raw.json").read_text()
        )
        baseline = json.loads((ROOT / "infra/.generated/d4/baseline.json").read_text())
        intent = authorize(
            parse(json.dumps(proposal)),
            list(records.values()),
            raw_current[proposal["resource"]],
            baseline,
            "sre-demo",
        )
        result.update(
            policy_authorized=True,
            disposition="plan_only",
            action_intent=intent,
            wrong_authorized_plan=not result["root_action_proxy_correct"],
            disposition_correct=result["root_action_proxy_correct"]
            and case.expected_disposition == "recover",
        )
    except (ValueError, KeyError, TypeError) as exc:
        result.update(
            policy_authorized=False,
            disposition="blocked_by_common_policy",
            disposition_correct=case.expected_disposition == "escalate",
            failure_category="common_policy_rejected",
            policy_error=str(exc)[:300],
        )
    return result


def trial(case_id, variant, repeat=0, *, capture_override=None, budget_override=None):
    from cloudsre.experiment_views import load_records

    case = case_by_id(case_id)
    capture = capture_override
    if capture is None:
        raise ValueError("D6 requires an explicit fresh capture")
    if not capture.get("passed") or not capture.get("scoring_ready"):
        raise ValueError("Case lacks a scoring-ready capture")
    snapshot = WORK / "snapshots" / capture["snapshot_id"]
    records = load_records(snapshot)
    if capture.get("records_sha256") != {
        kind: digest(record) for kind, record in records.items()
    }:
        raise ValueError("Frozen complete snapshot record hash mismatch")
    incident_hash = digest(json.loads((snapshot / "incident.json").read_text()))
    if capture.get("incident_sha256") != incident_hash:
        raise ValueError("Frozen incident hash missing or mismatched")
    software = image_provenance()
    ident = uuid.uuid4().hex
    folder = WORK / "diagnostics" / ident
    folder.mkdir(parents=True)
    (folder / "incident.json").write_bytes((snapshot / "incident.json").read_bytes())
    budget = (
        budget_override
        if budget_override is not None
        else ModelBudget(
            ROOT / "evaluation/d5-runtime/budget-private/batch4.sqlite", 100
        )
    )
    protocol = settings(variant)
    token = budget.register(ident, max(1, protocol["max_provider_attempts"]))
    private = ROOT / "infra/.generated/d5"
    private.mkdir(parents=True, exist_ok=True)
    model = dotenv_values("/mnt/d/uclaude/model.env")
    environment = {
        "OPENAI_BASE_URL": "http://cloudsre-d5-budget-proxy:8080/v1",
        "MODEL_NAME": model["MODEL_NAME"],
        "CLOUDSRE_RUN_TOKEN": token,
        "CLOUDSRE_RUN_DIR": "/work",
        "CLOUDSRE_SNAPSHOT_DIR": "/snapshot",
        "CLOUDSRE_TOOL_PYTHON": "/app/.venv/bin/python",
        "PYTHONPATH": "/app/backend:/home/lee/sre-project/.venv-holmes/lib/python3.12/site-packages:/home/lee/sre-project/vendor/holmesgpt",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "HOME": "/tmp",
    }
    envfile = private / (ident + ".env")
    envfile.write_text(
        "".join(key + "=" + value + "\n" for key, value in environment.items())
    )
    envfile.chmod(0o600)
    name = "cloudsre-d5-diag-" + ident[:12]
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none" if variant == "B0" else "sre-infra_default",
        "--memory",
        "768m",
        "--user",
        "1000:1000",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=128m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        "--env-file",
        str(envfile),
        "-v",
        str(folder) + ":/work:rw",
        "-v",
        str(snapshot) + ":/snapshot:ro",
        "-v",
        str(ROOT / ".venv-holmes") + ":" + str(ROOT / ".venv-holmes") + ":ro",
        "-v",
        str(ROOT / "vendor/holmesgpt") + ":" + str(ROOT / "vendor/holmesgpt") + ":ro",
        software["image_id"],
        "/usr/local/bin/python",
        "/app/diagnosis/runner_d5.py",
        "/work",
        variant,
    ]
    result = {
        "id": ident,
        "case_id": case_id,
        "split": case.split,
        "variant": variant,
        "repeat_index": repeat,
        "model_seed_supported": False,
        "snapshot_id": capture["snapshot_id"],
        "started_unix": time.time(),
        "completed": False,
        "output_valid": False,
        "recovery_measured": False,
        "protocol": protocol,
        "diagnostic_image_id": software["image_id"],
        "diagnostic_build_manifest": software["embedded_build_manifest"],
        "baseline_sha256": hashlib.sha256(
            (ROOT / "infra/.generated/d4/baseline.json").read_bytes()
        ).hexdigest(),
        "snapshot_record_hashes": {
            kind: record["content_hash"] for kind, record in records.items()
        },
    }
    try:
        started = time.monotonic()
        process = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=195
        )
        (folder / "stdout.log").write_text(process.stdout.replace(token, "[RUN_TOKEN]"))
        (folder / "stderr.log").write_text(process.stderr.replace(token, "[RUN_TOKEN]"))
        result.update(
            exit_code=process.returncode, elapsed_seconds=time.monotonic() - started
        )
        proposal_path = folder / "proposal.json"
        proposal = (
            json.loads(proposal_path.read_text()) if proposal_path.exists() else None
        )
        result.update(
            proposal=proposal,
            scoring=score(case, capture, proposal, records),
            completed=True,
            output_valid=process.returncode == 0 and proposal is not None,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        result.update(
            error_type="TimeoutExpired", scoring=score(case, capture, None, records)
        )
    finally:
        own = [row for row in budget.summary()["attempts"] if row["run_id"] == ident]
        result.update(
            provider_http_attempts=len(own),
            known_usage_attempts=sum(bool(row["usage"]) for row in own),
            unknown_usage_attempts=sum(not row["usage"] for row in own),
            known_total_tokens=sum(
                (row["usage"] or {}).get("total_tokens", 0) or 0 for row in own
            ),
            cost_amount=None,
            budget_remaining=budget.summary()["remaining_slots"],
            ended_unix=time.time(),
        )
        for field in ("prompt_tokens", "completion_tokens"):
            values = [
                row["usage"][field]
                for row in own
                if isinstance(row.get("usage"), dict)
                and type(row["usage"].get(field)) is int
                and row["usage"][field] >= 0
            ]
            result["known_" + field] = sum(values)
            result["unknown_" + field + "_attempts"] = len(own) - len(values)
        (folder / "result.json").write_text(json.dumps(result, indent=2))
        (folder / "provider-attempts.json").write_text(json.dumps(own, indent=2))
        public = WORK / "results"
        public.mkdir(exist_ok=True)
        (public / (ident + ".json")).write_text(json.dumps(result, indent=2))
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "id",
                        "case_id",
                        "variant",
                        "output_valid",
                        "provider_http_attempts",
                        "known_total_tokens",
                        "budget_remaining",
                    )
                }
            ),
            flush=True,
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument(
        "--variant",
        choices=sorted(VARIANTS),
        required=True,
    )
    parser.add_argument("--repeat", type=int, default=0)
    args = parser.parse_args()
    trial(args.case, args.variant, args.repeat)


if __name__ == "__main__":
    main()
