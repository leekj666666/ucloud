"""A healthy HTTP health endpoint must not override failed business verification."""

import json
import subprocess
import time
import uuid
from pathlib import Path

from cloudsre.durable import now
from cloudsre.quality import alert_keys, monitor, request
from verify_d3_scenarios import command, incidents, runs

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "evaluation/runs" / ("d3-false-recovery-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    report = {
        "started_at": now(),
        "passed": False,
        "expected_outcome": "ESCALATED",
        "evaluator_cleanup_used": False,
    }
    before = json.loads(command("get", "service", "inventory", "-o", "json"))
    original = json.loads(command("get", "deployment", "inventory", "-o", "json"))
    injected = False
    try:
        assert request("/verify")["passed"]
        deadline = time.monotonic() + 150
        while alert_keys(monitor()):
            assert time.monotonic() < deadline
            time.sleep(2)
        previous = {row["id"] for row in incidents()}
        command("scale", "deployment/inventory", "--replicas=0")
        injected = True
        deadline = time.monotonic() + 300
        chosen = None
        while time.monotonic() < deadline:
            for row in incidents():
                if row["id"] in previous:
                    continue
                records = runs(row["id"])
                if not records or records[-1]["payload"].get("schema_version") != 3:
                    continue
                found = records[-1]
                sample = (
                    ROOT
                    / "evaluation/d3-runtime/state"
                    / found["id"]
                    / "recovery-1/samples.jsonl"
                )
                if (
                    found["phase"] == "VERIFYING"
                    and sample.exists()
                    and sample.stat().st_size
                ):
                    chosen = row
                    break
            if chosen:
                break
            time.sleep(0.5)
        assert chosen, "No active recovery window reached"
        command(
            "patch",
            "service",
            "inventory",
            "--type=json",
            "-p",
            json.dumps(
                [{"op": "replace", "path": "/spec/ports/0/targetPort", "value": 8017}]
            ),
        )
        report["second_fault_at"] = now()
        health = subprocess.check_output(
            [
                "docker",
                "exec",
                "sre-infra-verifier-api-1",
                "python",
                "-c",
                "import httpx; r=httpx.get('http://sre-dev-control-plane:30082/health'); print(r.status_code)",
            ],
            text=True,
        ).strip()
        report["health_http_status"] = int(health)
        report["independent_business"] = request("/verify")
        assert int(health) == 200 and not report["independent_business"]["passed"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            latest = runs(chosen["id"])[-1]
            if latest["phase"] in {"RESOLVED", "ESCALATED"}:
                break
            time.sleep(0.5)
        report["journal"] = latest["payload"]
        assert latest["phase"] == "ESCALATED"
        assert latest["payload"]["ticket"]["status"] == "OPEN"
        assert not latest["payload"]["quality"]["passed"]
        report["passed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:600]
        raise
    finally:
        if injected:
            subprocess.run(
                ["docker", "stop", "cloudsre-worker-d3-1", "cloudsre-worker-d3-2"],
                capture_output=True,
                check=True,
            )
            for kind, old in (("service", before), ("deployment", original)):
                current = json.loads(command("get", kind, "inventory", "-o", "json"))
                command(
                    "patch",
                    kind,
                    "inventory",
                    "--type=json",
                    "-p",
                    json.dumps(
                        [
                            {
                                "op": "test",
                                "path": "/metadata/resourceVersion",
                                "value": current["metadata"]["resourceVersion"],
                            },
                            {"op": "replace", "path": "/spec", "value": old["spec"]},
                        ]
                    ),
                )
            command("rollout", "status", "deployment/inventory", "--timeout=120s")
            report["evaluator_cleanup_used"] = True
        report["ended_at"] = now()
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        print(
            json.dumps(
                {
                    "run_id": folder.name,
                    "passed": report["passed"],
                    "expected_outcome": "ESCALATED",
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
