"""Full D4 checks with an isolated real database and locked frontend build."""

import json
import os
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = Path(
    os.environ.get(
        "CLOUDSRE_NODE_BIN", str(Path.home() / "sre-tools/node-v24.22.1-linux-x64/bin")
    )
)


def main():
    folder = ROOT / "evaluation/runs" / ("d4-ci-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    env = {
        **os.environ,
        "PYTHONPATH": "backend",
        "CLOUDSRE_QUALITY_RESULT": str(folder / "base-quality.json"),
        "PATH": str(NODE) + ":" + os.environ["PATH"],
    }
    result = {"passed": False, "checks": {}}
    try:
        subprocess.run(
            [str(ROOT / ".venv/bin/python"), "scripts/quality_d1.py"],
            cwd=ROOT,
            env=env,
            check=True,
        )
        base = json.loads((folder / "base-quality.json").read_text())
        junit = Path(base["log"]).parent / "pytest.xml"
        cases = list(ET.parse(junit).getroot().iter("testcase"))
        assert len(cases) >= 113
        assert all(
            not any(c.tag in {"skipped", "failure", "error"} for c in case)
            for case in cases
        )
        required = {
            "test_concurrent_create_has_one_control_and_one_audit",
            "test_opposite_decisions_race_has_one_immutable_winner",
            "test_stop_accepted_before_dispatch_prevents_write",
            "test_stop_waits_for_inflight_dispatch_and_fences_following_write",
            "test_pending_control_task_is_not_starved_by_uncontrolled_alerts",
        }
        actual = {
            c.attrib["name"]
            for c in cases
            if "test_controls_postgres" in c.attrib.get("classname", "")
        }
        assert required <= actual
        assert any(
            c.attrib["name"]
            == "test_recovery_readiness_resets_on_delayed_alert_and_business_failure"
            for c in cases
        )
        assert (
            sum(
                "test_diagnostic_cancellation" in c.attrib.get("classname", "")
                for c in cases
            )
            == 3
        )
        result["checks"]["backend"] = {
            "passed": True,
            "tests": len(cases),
            "skipped": 0,
            "base": base,
        }
        commands = [
            (
                "d3_image",
                [
                    "docker",
                    "build",
                    "-t",
                    "cloudsre-agent:d3-check",
                    "-f",
                    "deploy/Dockerfile.d3",
                    ".",
                ],
                ROOT,
            ),
            (
                "d3_imports",
                [
                    "docker",
                    "run",
                    "--rm",
                    "-e",
                    "CLOUDSRE_DIAG_WORK=/tmp/diagnoses",
                    "--entrypoint",
                    "python",
                    "cloudsre-agent:d3-check",
                    "-c",
                    "from cloudsre.worker_v3 import process; from cloudsre.diagnosis_jobs import app",
                ],
                ROOT,
            ),
            (
                "alert_rules",
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "/bin/promtool",
                    "-v",
                    str(ROOT / "infra/rules.yml") + ":/etc/prometheus/rules.yml:ro",
                    "-v",
                    str(ROOT / "tests/prometheus_d3.yml") + ":/tests/rules.yml:ro",
                    "prom/prometheus@sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2",
                    "test",
                    "rules",
                    "/tests/rules.yml",
                ],
                ROOT,
            ),
            (
                "frontend_install",
                [str(NODE / "npm"), "ci", "--no-audit", "--no-fund"],
                ROOT / "frontend",
            ),
            ("frontend_build", [str(NODE / "npm"), "run", "build"], ROOT / "frontend"),
            (
                "manifest",
                [str(ROOT / ".venv/bin/python"), "scripts/build_d3_manifest.py"],
                ROOT,
            ),
            (
                "d4_image",
                [
                    "docker",
                    "build",
                    "-t",
                    "cloudsre-agent:d4",
                    "-f",
                    "deploy/Dockerfile.d4",
                    ".",
                ],
                ROOT,
            ),
            (
                "d4_imports",
                [
                    "docker",
                    "run",
                    "--rm",
                    "-e",
                    "CLOUDSRE_DIAG_WORK=/tmp/diagnoses",
                    "--entrypoint",
                    "python",
                    "cloudsre-agent:d4",
                    "-c",
                    "from pathlib import Path; from cloudsre.worker_v3 import process; from cloudsre.diagnosis_jobs import app; assert Path('/app/frontend/dist/index.html').is_file()",
                ],
                ROOT,
            ),
        ]
        with (folder / "checks.log").open("w") as log:
            for name, args, cwd in commands:
                subprocess.run(
                    args,
                    cwd=cwd,
                    env=env,
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                result["checks"][name] = {"passed": True}
                print(name + " passed", flush=True)
        result["passed"] = True
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc)[:1000])
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(result, indent=2))
        (ROOT / "evaluation/d4-quality-latest.json").write_text(
            json.dumps({"folder": str(folder), **result}, indent=2)
        )
        print(
            json.dumps({"folder": str(folder), "passed": result["passed"]}), flush=True
        )


if __name__ == "__main__":
    main()
