"""Run complete regression against an explicitly separate PostgreSQL database."""

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from dotenv import dotenv_values
from sqlalchemy import URL, create_engine, text

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "sre_d5_regression_20261025"


def main():
    values = dotenv_values(ROOT / "infra/.env")
    connection = URL.create(
        "postgresql+psycopg",
        username="sre",
        password=values["POSTGRES_PASSWORD"],
        host="localhost",
        port=15432,
        database="postgres",
    )
    engine = create_engine(connection, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": DATABASE}
        ).scalar()
        if not exists:
            conn.execute(text('CREATE DATABASE "' + DATABASE + '"'))
    engine.dispose()
    folder = ROOT / "evaluation/d5-runtime/regression"
    folder.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "SRE_TEST_DATABASE_URL": connection.set(database=DATABASE).render_as_string(
            hide_password=False
        ),
        "SRE_CI_EVIDENCE_DIR": str(folder),
        "PYTHONPATH": "backend",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-ra",
            "--junitxml=" + str(folder / "pytest.xml"),
        ],
        env=environment,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    secret = values["POSTGRES_PASSWORD"]
    output = (result.stdout + result.stderr).replace(secret, "[DATABASE_PASSWORD]")
    (folder / "pytest.log").write_text(output)
    print(output)
    cases = list(ElementTree.parse(folder / "pytest.xml").getroot().iter("testcase"))
    rejected = [
        case.attrib["name"]
        for case in cases
        if any(child.tag in {"skipped", "failure", "error"} for child in case)
    ]
    summary = {
        "exit_code": result.returncode,
        "tests": len(cases),
        "nonpassing": rejected,
        "dedicated_database": DATABASE,
        "passed": result.returncode == 0 and not rejected,
    }
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
