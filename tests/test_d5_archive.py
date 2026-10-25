import io
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import archive_d5_evidence as delivery

API_KEY = "test-private-api-key-9e2d21"
RUN_TOKEN = "test-private-run-token-5ff339"
DATABASE = "postgresql://test:private-password-21577@localhost/demo"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def fake_repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    write(root / ".gitignore", ".env*\nevaluation/\ninfra/.generated/\nvendor/\n")
    write(root / "app.py", "print('public source')\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    write(root / "new-source.py", "# nonignored and untracked\n")
    write(root / "vendor/never.py", "excluded vendor dependency\n")
    model_env = write(
        tmp_path / "model.env",
        "DEEPSEEK_API_KEY=" + API_KEY + "\nOPENAI_BASE_URL=https://api.deepseek.com\n",
    )
    write(root / "infra/.env", "DATABASE_URL=" + DATABASE + "\n")
    write(
        root / "infra/.generated/d5/run.env", "CLOUDSRE_RUN_TOKEN=" + RUN_TOKEN + "\n"
    )
    runtime = root / "evaluation/d5-runtime"
    write(
        runtime / "diagnostics/one/log.json",
        json.dumps({"key": API_KEY, "run": RUN_TOKEN}),
    )
    write(
        root / "evaluation/d5-private/captures/one/current-raw.json",
        json.dumps({"url": DATABASE}),
    )
    write(
        runtime / "budget-private/private-only.txt",
        "Do not copy any nontrace private budget files",
    )
    write(
        runtime / "budget-private/traces/a/request.json",
        json.dumps({"messages": [{"content": "public message"}]}),
    )
    write(
        runtime / "delivery-status.json", json.dumps({"complete": False, "stage": "D5"})
    )
    write(root / "evaluation/d5-controls.json", json.dumps({"passed": True}))
    write(root / "evaluation/d5-isolation.json", json.dumps({"passed": True}))
    write(
        root / "evaluation/runs/d5-controls-one/result.json",
        json.dumps({"passed": True}),
    )
    database = runtime / "budget-private/batch.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE budget(id INTEGER PRIMARY KEY,max_attempts INTEGER);
            INSERT INTO budget VALUES(1,100);
            CREATE TABLE runs(id TEXT,token_hash TEXT,max_attempts INTEGER);
            INSERT INTO runs VALUES('one','do-not-copy-this-token-hash',4);
            CREATE TABLE attempts(id TEXT,run_id TEXT,reserved_at TEXT,request_sha256 TEXT,state TEXT,http_status INTEGER,usage TEXT,error_type TEXT,elapsed_seconds REAL);
            CREATE TABLE attempt_metadata(id TEXT,settings TEXT);
            INSERT INTO attempts VALUES('a','one','2026-10-25','request-hash','finished',200,'{"total_tokens":12}',NULL,1.2);
            INSERT INTO attempts VALUES('b','one','2026-10-25','request-hash','reserved_unknown',NULL,NULL,NULL,NULL);
            INSERT INTO attempt_metadata VALUES('a','{"temperature":0}');
        """)
    return root, model_env, database


def test_d5_archive_redacts_evidence_retains_source_hashes_and_never_copies_private_budget(
    tmp_path,
):
    root, model_env, database = fake_repository(tmp_path)
    budget_hash = delivery.sha(database.read_bytes())
    evidence = root / "evaluation/d5-runtime/diagnostics/one/log.json"
    before = evidence.read_bytes()
    result = delivery.archive(
        root, tmp_path / "archives", model_env=model_env, stamp="first"
    )
    out = Path(result["archive"])
    manifest = json.loads((out / "manifest.json").read_text())
    entries = {row["path"]: row for row in manifest["files"]}
    assert entries["runtime/diagnostics/one/log.json"][
        "canonical_sha256"
    ] == delivery.sha(before)
    assert entries["runtime/diagnostics/one/log.json"]["redaction_count"] == 2
    assert evidence.read_bytes() == before
    assert delivery.sha(database.read_bytes()) == budget_hash
    assert (out / "source/new-source.py").is_file()
    assert not (out / "source/vendor").exists()
    assert (out / "evaluator-private/captures/one/current-raw.json").is_file()
    assert (out / "model-http-traces/a/request.json").is_file()
    assert (out / "evaluation/runs/d5-controls-one/result.json").is_file()
    assert not any(
        "budget-private" in str(path.relative_to(out)) for path in out.rglob("*")
    )
    assert not any(
        path.suffix == ".env" or path.suffix == ".sqlite" for path in out.rglob("*")
    )
    for path in out.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert all(
                value.encode() not in raw
                for value in (
                    API_KEY,
                    RUN_TOKEN,
                    DATABASE,
                    "do-not-copy-this-token-hash",
                )
            )
    for row in manifest["files"]:
        raw = (out / row["path"]).read_bytes()
        assert len(raw) == row["bytes"] and delivery.sha(raw) == row["archive_sha256"]
    summary = json.loads((out / "metadata/budget-public-summary.json").read_text())
    assert summary["used_slots"] == 2 and summary["unknown_usage_attempts"] == 1
    assert summary["known_total_tokens"] == 12 and summary["remaining_slots"] == 98
    assert result["D5_completion_state"] == "partial_or_unverified"
    assert result["manifest_sha256"] == delivery.sha(
        (out / "manifest.json").read_bytes()
    )
    with pytest.raises(FileExistsError):
        delivery.archive(
            root, tmp_path / "archives", model_env=model_env, stamp="first"
        )


def test_d5_archive_rejects_source_credentials_without_printing_or_modifying_them(
    tmp_path,
):
    root, model_env, _ = fake_repository(tmp_path)
    source = write(root / "bad-source.py", "API_KEY = '" + API_KEY + "'\n")
    raw = source.read_bytes()
    with pytest.raises(delivery.ArchiveRejected) as failure:
        delivery.archive(root, tmp_path / "archives", model_env=model_env)
    assert API_KEY not in str(failure.value)
    assert "bad-source.py" in str(failure.value)
    assert source.read_bytes() == raw


def test_d5_archive_inspects_and_redacts_zip_members_and_omits_dotenv(tmp_path):
    root, model_env, _ = fake_repository(tmp_path)
    ci = tmp_path / "ci"
    ci.mkdir()
    artifact = ci / "artifact.zip"
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("logs/report.txt", "key=" + API_KEY)
        output.writestr(".env", "SECRET=" + API_KEY)
        output.writestr("../escape", "not materialized")
    canonical = delivery.sha(artifact.read_bytes())
    result = delivery.archive(root, tmp_path / "archives", model_env=model_env, ci=[ci])
    out = Path(result["archive"])
    copied = out / "ci/0-ci/artifact.zip"
    with zipfile.ZipFile(copied) as output:
        assert output.namelist() == ["logs/report.txt"]
        assert output.read("logs/report.txt") == b"key=[REDACTED]"
    assert delivery.sha(artifact.read_bytes()) == canonical
    manifest = json.loads((out / "manifest.json").read_text())
    row = next(row for row in manifest["files"] if row["path"].endswith("artifact.zip"))
    assert row["canonical_sha256"] == canonical
    assert row["archive_sha256"] != canonical and row["redaction_count"] == 1
    assert len(row["excluded_zip_members"]) == 2


def test_d5_archive_refuses_credentials_in_binary_or_zip_metadata():
    for name, data in (
        ("trace.bin", b"\xff" + API_KEY.encode()),
        ("artifact.zip", None),
    ):
        if data is None:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as output:
                output.writestr("test.txt", "safe")
                output.comment = API_KEY.encode()
            data = stream.getvalue()
        with pytest.raises(delivery.ArchiveRejected) as failure:
            delivery.sanitize(
                data, name=name, kind="evidence", secrets=[API_KEY.encode()]
            )
        assert API_KEY not in str(failure.value)


def test_d5_archive_status_requires_actual_finished_matrix_not_only_status(tmp_path):
    matrix = tmp_path / "runtime/matrix"
    plan = {"tasks": [{"id": "one"}]}
    write(matrix / "plan.json", json.dumps(plan))
    state = {
        "status": "finished",
        "plan_sha256": delivery.sha(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ),
        "tasks": {"one": {"status": "finished", "result_id": "a"}},
    }
    write(matrix / "state.json", json.dumps(state))
    first = delivery.matrix_status(tmp_path, {"complete": True})
    assert first["completion_state"] == "partial_or_unverified"
    write(tmp_path / "runtime/results/a.json", "{}")
    second = delivery.matrix_status(tmp_path, {"complete": True})
    assert second["completion_state"].startswith("complete_declared")
    assert (
        delivery.matrix_status(tmp_path, {"complete": False})["completion_state"]
        == "partial_or_unverified"
    )


def test_d5_archive_excludes_kubeconfig_and_refuses_old_archive_destination(tmp_path):
    data = b"kind: Config\nusers:\n- name: real\n  user:\n    token: never-copy\n"
    with pytest.raises(delivery.ArchiveRejected, match="Kubeconfig"):
        delivery.sanitize(data, name="credential.yaml", kind="evidence", secrets=[])
    root, model_env, _ = fake_repository(tmp_path)
    with pytest.raises(delivery.ArchiveRejected, match="D3/D4"):
        delivery.archive(root, tmp_path / "d4-evidence", model_env=model_env)
    assert not (tmp_path / "d4-evidence").exists()


def test_d5_known_values_include_encodings_and_exclude_public_api_base_url(tmp_path):
    root, model_env, _ = fake_repository(tmp_path)
    secrets, _ = delivery.secret_values(root, model_env)
    assert API_KEY.encode() in secrets and RUN_TOKEN.encode() in secrets
    assert b"private-password-21577" in secrets
    assert b"https://api.deepseek.com" not in secrets
    assert (
        delivery.sanitize(
            b'"token":"' + API_KEY.encode() + b'"',
            name="response.json",
            kind="evidence",
            secrets=secrets,
        )[0]
        == b'"token":"[REDACTED]"'
    )
