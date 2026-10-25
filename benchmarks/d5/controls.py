"""Live non-model controls for the common full business evaluator."""

import json
import subprocess
import uuid

from cloudsre.durable import now
from cloudsre.quality import run_window

from .capture import prepare
from .catalog import DEV
from .live import ROOT, Fixture, configure, exclusive, wait_healthy


def main():
    configure()
    folder = ROOT / "evaluation/runs" / ("d5-controls-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    result = {
        "started_at": now(),
        "passed": False,
        "model_calls": 0,
        "controls": {},
        "shared_fault_window_for_noop_and_health_claim": True,
    }
    baseline = json.loads((ROOT / "infra/.generated/d4/baseline.json").read_text())[
        "quality"
    ]
    with exclusive():
        prepare()
        wait_healthy(folder)
        fixture = Fixture(DEV[2], folder / "fixture")
        try:
            fixture.inject()
            fixture.drain()
            code = "import httpx;print(httpx.get('http://sre-dev-control-plane:30082/health',timeout=10).status_code)"
            health = int(
                subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        "sre-infra-verifier-api-1",
                        "python",
                        "-c",
                        code,
                    ],
                    text=True,
                ).strip()
            )
            fault = run_window(folder / "fault-window", baseline)
            assert health == 200 and not fault["passed"]
            result["controls"]["no_operation"] = {
                "external_recovery": False,
                "quality": fault,
            }
            result["controls"]["health_only_claim"] = {
                "health_status": health,
                "agent_claimed_success": True,
                "external_recovery": False,
                "false_success_detected": True,
            }
            fixture.cleanup()
            wait_healthy(folder)
            reference = run_window(folder / "reference-window", baseline)
            assert reference["passed"]
            result["controls"]["reference_repair"] = {
                "actor": "evaluator reference action, not an Agent result",
                "external_recovery": True,
                "quality": reference,
            }
            result["passed"] = True
        finally:
            if fixture.record["active"]:
                fixture.cleanup()
            result["ended_at"] = now()
            result["folder"] = str(folder)
            (folder / "result.json").write_text(json.dumps(result, indent=2))
            (ROOT / "evaluation/d5-controls.json").write_text(
                json.dumps(result, indent=2)
            )
            print(
                json.dumps(
                    {
                        "folder": str(folder),
                        "passed": result["passed"],
                        "model_calls": 0,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
