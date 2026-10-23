"""Local equivalent of CI, using an isolated disposable PostgreSQL container."""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
NAME = "sre-d1-ci-" + uuid.uuid4().hex[:10]
IMAGE = (
    "postgres@sha256:b8faeff7712663c2773a190181e1246e37e364b887611c04658c28b44e2408fe"
)


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def main():
    folder = ROOT / "evaluation/runs" / NAME
    folder.mkdir(parents=True)
    report = {"container": NAME, "checks": {}, "model_calls": 0}
    log = (folder / "checks.log").open("w")
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                NAME,
                "--memory",
                "192m",
                "-e",
                "POSTGRES_USER=ci",
                "-e",
                "POSTGRES_DB=ci",
                "-e",
                "POSTGRES_PASSWORD=ci-only-password",
                "-p",
                "127.0.0.1::5432",
                IMAGE,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        port = (
            subprocess.check_output(["docker", "port", NAME, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        dsn = "postgresql://ci:ci-only-password@localhost:" + port + "/ci"
        for _ in range(60):
            try:
                with psycopg.connect(dsn):
                    break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            raise RuntimeError("CI PostgreSQL not ready")
        env = {
            **os.environ,
            "PYTHONPATH": "backend",
            "SRE_TEST_DATABASE_URL": dsn.replace(
                "postgresql://", "postgresql+psycopg://"
            ),
        }
        env["SRE_CI_EVIDENCE_DIR"] = str(folder)
        for name, args in [
            (
                "lint",
                [
                    str(ROOT / ".venv/bin/ruff"),
                    "check",
                    "backend",
                    "tests",
                    "scripts",
                    "diagnosis",
                ],
            ),
            (
                "tests_with_real_postgres",
                [
                    str(ROOT / ".venv/bin/pytest"),
                    "tests",
                    "-q",
                    "-ra",
                    "--junitxml=" + str(folder / "pytest.xml"),
                ],
            ),
            (
                "image_build",
                [
                    "docker",
                    "build",
                    "-t",
                    "cloudsre-app:d1-complete",
                    "-f",
                    "deploy/Dockerfile",
                    ".",
                ],
            ),
            (
                "container_imports",
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "python",
                    "cloudsre-app:d1-complete",
                    "-c",
                    "from cloudsre.demo import create_demo; from cloudsre.probe import app",
                ],
            ),
        ]:
            started = time.monotonic()
            run(args, env=env, stdout=log, stderr=subprocess.STDOUT)
            report["checks"][name] = {
                "passed": True,
                "seconds": time.monotonic() - started,
            }
            print(name + " passed", flush=True)
        report["passed"] = True
    finally:
        report["test_database_isolated"] = True
        report["log"] = str(folder / "checks.log")
        # NAME is created by this run; remove only that container and its anonymous volumes.
        cleanup = subprocess.run(
            ["docker", "rm", "-f", "-v", NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        report["cleanup"] = {
            "container_and_anonymous_volumes_removed": cleanup.returncode == 0
        }
        if cleanup.returncode:
            report["passed"] = False
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        Path("/mnt/d/uclaude/d1-quality.json").write_text(json.dumps(report, indent=2))
        log.close()
    assert report["cleanup"]["container_and_anonymous_volumes_removed"], report


if __name__ == "__main__":
    main()
