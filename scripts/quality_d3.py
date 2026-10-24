"""Full local D3 CI with isolated PostgreSQL, no skips, image and rule validation."""

import json
import os
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "evaluation/runs" / ("d3-ci-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    env = {
        **os.environ,
        "PYTHONPATH": "backend",
        "CLOUDSRE_QUALITY_RESULT": str(folder / "base-quality.json"),
    }
    py = str(ROOT / ".venv/bin/python")
    report = {"passed": False, "checks": {}}
    try:
        subprocess.run([py, "scripts/quality_d1.py"], cwd=ROOT, env=env, check=True)
        base = json.loads((folder / "base-quality.json").read_text())
        cases = list(
            ET.parse(Path(base["log"]).parent / "pytest.xml").getroot().iter("testcase")
        )
        assert len(cases) >= 91
        assert all(
            not any(c.tag in {"skipped", "failure", "error"} for c in case)
            for case in cases
        )
        report["checks"]["tests"] = {
            "passed": True,
            "count": len(cases),
            "skipped": 0,
            "evidence": base,
        }
        subprocess.run(
            [py, "scripts/build_d3_manifest.py"], cwd=ROOT, env=env, check=True
        )
        commands = {
            "d3_image": [
                "docker",
                "build",
                "-t",
                "cloudsre-agent:d3",
                "-f",
                "deploy/Dockerfile.d3",
                ".",
            ],
            "d3_imports": [
                "docker",
                "run",
                "--rm",
                "-e",
                "CLOUDSRE_DIAG_WORK=/tmp/diagnoses",
                "--entrypoint",
                "python",
                "cloudsre-agent:d3",
                "-c",
                "from cloudsre.worker_v3 import process; from cloudsre.diagnosis_jobs import app",
            ],
        }
        image = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Image}}", "sre-infra-prometheus-1"],
            text=True,
        ).strip()
        commands["alert_rules"] = [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/promtool",
            "-v",
            str(ROOT / "infra/rules.yml") + ":/etc/prometheus/rules.yml:ro",
            "-v",
            str(ROOT / "tests/prometheus_d3.yml") + ":/tests/rules.yml:ro",
            image,
            "test",
            "rules",
            "/tests/rules.yml",
        ]
        with (folder / "checks.log").open("w") as log:
            for name, args in commands.items():
                subprocess.run(
                    args,
                    cwd=ROOT,
                    env=env,
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                report["checks"][name] = {"passed": True}
        report["passed"] = True
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc)[:1000])
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        (ROOT / "evaluation/d3-quality-latest.json").write_text(
            json.dumps({"folder": str(folder), **report}, indent=2)
        )
        print(
            json.dumps({"folder": str(folder), "passed": report["passed"]}), flush=True
        )


if __name__ == "__main__":
    main()
