"""Create a new, verified D5 archive without modifying runtime or older archives.

Only exact known credentials (including their common encodings) are audited.
Credential-free archival is not a claim that D5 experiments passed. Run inside
WSL; this script never invokes a model, Docker, Kubernetes, or a database write.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, quote_plus, unquote, urlsplit

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ARCHIVES = Path("/mnt/d/uclaude/d5-evidence")
REDACTED = b"[REDACTED]"


class ArchiveRejected(ValueError):
    """A safe, value-free description suitable for a command-line error."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def safe_name(name, secrets):
    result = os.fsencode(str(name))
    for value in secrets:
        result = result.replace(value, REDACTED)
    return os.fsdecode(result)


def secret_values(root, model_env):
    """Return values internally, never expose values or hashes in reports."""
    if not model_env.is_file():
        raise ArchiveRejected(
            "Model credential reference file is absent; known-value scan cannot be certified"
        )
    files = {model_env, *root.glob(".env*"), *(root / "infra").glob(".env*")}
    generated = root / "infra/.generated"
    files.update(generated.rglob("*.env"))
    files.update(generated.rglob(".env*"))
    values = set()
    inputs = 0
    for path in files:
        if not path.is_file() or path.name.endswith(("example", "sample", "template")):
            continue
        inputs += 1
        for key, value in dotenv_values(path, interpolate=False).items():
            if not value:
                continue
            if any(
                word in key.upper()
                for word in (
                    "API_KEY",
                    "APIKEY",
                    "PASSWORD",
                    "PASSWD",
                    "SECRET",
                    "TOKEN",
                    "DATABASE_URL",
                    "DB_URL",
                )
            ):
                values.add(value)
            if "://" in value:
                try:
                    parsed = urlsplit(value)
                    if parsed.password:
                        values.update((value, unquote(parsed.password)))
                except ValueError:
                    pass
    # Kubeconfigs are never copied, but their actual credentials are checked.
    for path in generated.rglob("*.yaml"):
        if not path.is_file():
            continue
        for document in yaml.safe_load_all(path.read_text()):
            if not isinstance(document, dict):
                continue
            for user in document.get("users", []):
                for key, value in user.get("user", {}).items():
                    if key in {"token", "password", "client-key-data"} and isinstance(
                        value, str
                    ):
                        values.add(value)
    if not values:
        raise ArchiveRejected(
            "No known credential values available for the required scan"
        )
    encoded = set()
    for value in values:
        if not value or value == "[REDACTED]":
            continue
        raw = value.encode()
        encoded.update(
            (
                raw,
                json.dumps(value, ensure_ascii=False)[1:-1].encode(),
                quote(value, safe="").encode(),
                quote_plus(value).encode(),
                base64.b64encode(raw),
            )
        )
    return sorted(encoded, key=len, reverse=True), {
        "credential_input_files_read": inputs,
        "known_values_checked": len(values),
        "known_representations_checked": len(encoded),
    }


def forbidden(relative):
    path = PurePosixPath(str(relative).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return True
    if any(
        part in {".git", ".generated", "vendor", "node_modules", "__pycache__"}
        or part.startswith(".venv")
        for part in path.parts
    ):
        return True
    name = path.name.lower()
    return bool(
        name.startswith(".env")
        or name.endswith(".env")
        or "kubeconfig" in name
        or path.suffix.lower()
        in {".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"}
    )


def kubeconfig(data, suffix):
    if suffix.lower() not in {".yaml", ".yml", ".json"}:
        return False
    try:
        body = yaml.safe_load(data)
    except (ValueError, UnicodeError, yaml.YAMLError):
        return False
    return (
        isinstance(body, dict)
        and body.get("kind") == "Config"
        and any(key in body for key in ("users", "clusters", "contexts"))
    )


def sanitize(data, *, name, kind, secrets, depth=0):
    """Exact replacement in text/ZIP evidence; source changes are refused."""
    safe = safe_name(name, secrets)
    if os.fsencode(str(name)) != os.fsencode(safe):
        raise ArchiveRejected("Credential in a filename; refused: " + safe)
    suffix = PurePosixPath(str(name)).suffix.lower()
    if kubeconfig(data, suffix):
        raise ArchiveRejected("Kubeconfig content refused: " + safe)
    if suffix in {".gz", ".tgz", ".tar", ".7z", ".bz2", ".xz", ".rar"}:
        raise ArchiveRejected(
            "Opaque archive requires separate inspected evidence: " + safe
        )
    if suffix == ".zip" or data.startswith(b"PK\x03\x04"):
        if depth >= 3:
            raise ArchiveRejected("Nested ZIP inspection depth exceeded: " + safe)
        target = io.BytesIO()
        changes = 0
        omitted = []
        with (
            zipfile.ZipFile(io.BytesIO(data)) as original,
            zipfile.ZipFile(target, "w") as output,
        ):
            if any(value in original.comment for value in secrets):
                raise ArchiveRejected(
                    "Known credential in ZIP metadata; refused: " + safe
                )
            if sum(item.file_size for item in original.infolist()) > 500_000_000:
                raise ArchiveRejected("ZIP inspection size exceeded: " + safe)
            for item in original.infolist():
                member = safe + "!" + item.filename
                if any(
                    value in item.comment or value in item.extra for value in secrets
                ):
                    raise ArchiveRejected(
                        "Known credential in ZIP member metadata; refused: "
                        + safe_name(member, secrets)
                    )
                if forbidden(item.filename):
                    omitted.append(safe_name(member, secrets))
                    continue
                if item.flag_bits & 1:
                    raise ArchiveRejected(
                        "Encrypted ZIP member cannot be audited: "
                        + safe_name(member, secrets)
                    )
                raw = original.read(item)
                cleaned, count, skipped = sanitize(
                    raw, name=member, kind=kind, secrets=secrets, depth=depth + 1
                )
                output.writestr(item, cleaned)
                changes += count
                omitted.extend(skipped)
        if kind == "source" and (changes or omitted):
            raise ArchiveRejected(
                "Source archive would require changes; refused: " + safe
            )
        return (target.getvalue() if changes or omitted else data), changes, omitted
    matches = [(value, data.count(value)) for value in secrets if value in data]
    if not matches:
        return data, 0, []
    if kind == "source":
        raise ArchiveRejected(
            "Known credential found in source; canonical file unchanged: " + safe
        )
    try:
        data.decode("utf-8")
    except UnicodeError:
        raise ArchiveRejected(
            "Known credential in binary evidence; refused: " + safe
        ) from None
    cleaned = data
    count = 0
    for value in secrets:
        count += cleaned.count(value)
        cleaned = cleaned.replace(value, REDACTED)
    return cleaned, count, []


def public_budget(database):
    """A read-only snapshot that never selects runs.token_hash or copies SQLite."""
    if not database.is_file():
        return {
            "available": False,
            "reason": "Budget database absent",
            "cost_amount": None,
        }
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        limit = connection.execute(
            "SELECT max_attempts FROM budget WHERE id=1"
        ).fetchone()[0]
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id,run_id,reserved_at,request_sha256,state,http_status,usage,error_type,elapsed_seconds FROM attempts ORDER BY reserved_at,id"
            )
        ]
        settings = {
            row["id"]: json.loads(row["settings"])
            for row in connection.execute("SELECT id,settings FROM attempt_metadata")
        }
    known = []
    for row in rows:
        row["usage"] = json.loads(row["usage"]) if row["usage"] else None
        row["generation_settings"] = settings.get(row["id"])
        if isinstance(row["usage"], dict):
            known.append(row)
    return {
        "available": True,
        "read_only_projection": True,
        "limit": limit,
        "used_slots": len(rows),
        "remaining_slots": limit - len(rows),
        "known_usage_attempts": len(known),
        "unknown_usage_attempts": len(rows) - len(known),
        "known_total_tokens": sum(
            row["usage"].get("total_tokens", 0) or 0 for row in known
        ),
        "cost_amount": None,
        "cost_state": "unknown",
        "attempts": rows,
        "semantics": "Conservative reservations include uncertain interrupted attempts; no budget reset or database write",
    }


def matrix_status(archive, declaration):
    base = archive / "runtime/matrix"
    plan_path, state_path = base / "plan.json", base / "state.json"
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else {}
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    tasks = plan.get("tasks", [])
    entries = state.get("tasks", {})
    expected = {row["id"] for row in tasks}
    finished = sum(entries.get(key, {}).get("status") == "finished" for key in expected)
    missing_results = [
        key
        for key in expected
        if not (
            archive
            / "runtime/results"
            / (entries.get(key, {}).get("result_id", "missing") + ".json")
        ).is_file()
    ]
    plan_hash = sha(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
    consistent = (
        bool(tasks)
        and expected == set(entries)
        and finished == len(tasks)
        and not missing_results
        and state.get("status") == "finished"
        and state.get("plan_sha256") == plan_hash
    )
    declared = isinstance(declaration, dict) and declaration.get("complete") is True
    return {
        "supplied_status_declares_complete": declared,
        "matrix_state": state.get("status", "absent"),
        "planned_tasks": len(tasks),
        "finished_tasks": finished,
        "missing_result_files": missing_results,
        "matrix_structurally_finished": consistent,
        "completion_state": "complete_declared_by_supplied_status_with_finished_matrix"
        if declared and consistent
        else "partial_or_unverified",
        "all_D5_acceptance_gates_independently_rechecked_by_archiver": False,
        "meaning": "Preserves supplied status and actual archived matrix; credential/hash verification does not certify experimental success",
    }


def archive(
    root, destination_root, *, model_env, status=None, ci=(), deployment=(), stamp=None
):
    root, destination_root = root.resolve(), destination_root.resolve()
    if destination_root.is_relative_to(root) or any(
        part.lower() in {"d3-evidence", "d4-evidence"}
        for part in destination_root.parts
    ):
        raise ArchiveRejected(
            "Archive destination must be outside source and D3/D4 evidence"
        )
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    if "/" in stamp or "\\" in stamp or stamp in {"", ".", ".."}:
        raise ArchiveRejected("Invalid archive timestamp")
    destination = destination_root / stamp
    destination.mkdir(parents=True, exist_ok=False)
    secrets, scan_info = secret_values(root, model_env)
    manifest = []
    omitted = []
    redactions = []
    selected = {}

    def add_file(source, relative, kind):
        name = str(relative).replace("\\", "/")
        if forbidden(name):
            omitted.append(
                {
                    "path": safe_name(name, secrets),
                    "reason": "Credential/runtime dependency path excluded",
                }
            )
            return
        if source.is_symlink():
            raise ArchiveRejected("Symlink is not copied: " + safe_name(name, secrets))
        if source.is_file():
            if name in selected and selected[name][0] != source:
                raise ArchiveRejected(
                    "Archive destination collision: " + safe_name(name, secrets)
                )
            selected[name] = (source, kind)

    def add_tree(source, relative, kind):
        if source.is_file() or source.is_symlink():
            add_file(source, relative / source.name, kind)
        elif source.is_dir():
            for file in sorted(source.rglob("*")):
                if file.is_file() or file.is_symlink():
                    add_file(file, relative / file.relative_to(source), kind)

    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    ).split(b"\0")
    for raw in names:
        if raw:
            name = Path(os.fsdecode(raw))
            if name.parts[0] != "evaluation":
                add_file(root / name, Path("source") / name, "source")
    runtime = root / "evaluation/d5-runtime"
    for file in sorted(runtime.rglob("*")):
        if (
            file.is_file() or file.is_symlink()
        ) and "budget-private" not in file.relative_to(runtime).parts:
            add_file(file, Path("runtime") / file.relative_to(runtime), "evidence")
    add_tree(runtime / "budget-private/traces", Path("model-http-traces"), "evidence")
    add_tree(
        root / "evaluation/d5-private", Path("evaluator-private"), "evaluator-private"
    )
    for name in ("d5-controls.json", "d5-isolation.json"):
        add_file(root / "evaluation" / name, Path("evaluation") / name, "evidence")
    for folder in sorted((root / "evaluation/runs").glob("d5-controls-*")):
        add_tree(folder, Path("evaluation/runs") / folder.name, "evidence")
    for kind, paths in (("ci", ci), ("deployment", deployment)):
        for number, path in enumerate(paths):
            if not path.exists():
                raise ArchiveRejected(
                    "Requested evidence is absent: " + safe_name(path, secrets)
                )
            add_tree(path, Path(kind) / (str(number) + "-" + path.name), "evidence")
    status = status or runtime / "delivery-status.json"
    add_file(status, Path("metadata/supplied-status.json"), "evidence")
    for name, (source, kind) in sorted(selected.items()):
        raw = source.read_bytes()
        data, count, members = sanitize(raw, name=name, kind=kind, secrets=secrets)
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest.append(
            {
                "path": name,
                "origin": str(source),
                "kind": kind,
                "canonical_sha256": sha(raw),
                "archive_sha256": sha(data),
                "canonical_bytes": len(raw),
                "bytes": len(data),
                "redaction_count": count,
                "excluded_zip_members": members,
            }
        )
        if count or members:
            redactions.append(
                {
                    "path": name,
                    "redaction_count": count,
                    "excluded_zip_members": members,
                }
            )
    generated = {
        "metadata/budget-public-summary.json": public_budget(
            runtime / "budget-private/batch.sqlite"
        ),
        "metadata/source-version.json": {
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip(),
            "working_tree_status": subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True
            ).splitlines(),
            "source_selection": "git tracked and nonignored untracked files, with explicit credential/dependency exclusions",
        },
    }
    declaration_path = destination / "metadata/supplied-status.json"
    declaration = (
        json.loads(declaration_path.read_text()) if declaration_path.exists() else None
    )
    generated["metadata/archive-status.json"] = matrix_status(destination, declaration)
    generated["metadata/archive-scope.json"] = {
        "created_at": datetime.now(UTC).isoformat(),
        "source": str(root),
        "snapshot_consistency": "Per-file read snapshots; no claim of an atomic whole-runtime snapshot while writers are active",
        "evaluator_private": "Offline evaluator-only data. Never mount this archive or evaluator-private in an Agent",
        "excluded": "All dotenv/kubeconfig/generated credentials, private budget database and run token hashes; D3/D4 archives untouched",
        "omitted_paths": omitted,
    }
    generated["credential-scan.json"] = {
        **scan_info,
        "passed": True,
        "known_secret_matches_after_redaction": 0,
        "redacted_files": redactions,
        "canonical_sources_modified": False,
        "scan_scope": "Exact known values and common JSON/URL/base64 encodings; ZIP contents recursively inspected. Does not claim discovery of unknown credentials",
    }
    for name, value in generated.items():
        raw = json_bytes(value)
        data, count, members = sanitize(
            raw, name=name, kind="evidence", secrets=secrets
        )
        if count or members:
            raise ArchiveRejected(
                "Unexpected known credential in generated metadata: " + name
            )
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest.append(
            {
                "path": name,
                "origin": "generated_safe_projection",
                "kind": "metadata",
                "canonical_sha256": None,
                "archive_sha256": sha(data),
                "bytes": len(data),
                "redaction_count": 0,
            }
        )
    for entry in manifest:
        raw = (destination / entry["path"]).read_bytes()
        if sha(raw) != entry["archive_sha256"] or len(raw) != entry["bytes"]:
            raise ArchiveRejected("Archive read-back hash mismatch: " + entry["path"])
        clean, count, members = sanitize(
            raw, name=entry["path"], kind="source", secrets=secrets
        )
        if clean != raw or count or members:
            raise ArchiveRejected(
                "Archive read-back credential scan failed: " + entry["path"]
            )
    content = {
        "schema_version": 1,
        "hash_algorithm": "SHA256",
        "files": sorted(manifest, key=lambda row: row["path"]),
        "all_entries_read_back_and_verified": True,
        "credential_scan_passed": True,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_bytes(json_bytes(content))
    # The manifest excludes itself; its separately returned hash anchors it.
    reread = json.loads(manifest_path.read_bytes())
    if reread != content:
        raise ArchiveRejected("Manifest read-back differs")
    sanitize(
        manifest_path.read_bytes(), name="manifest.json", kind="source", secrets=secrets
    )
    return {
        "archive": str(destination),
        "files": len(manifest),
        "manifest_sha256": sha(manifest_path.read_bytes()),
        "all_manifest_hashes_verified": True,
        "known_secret_matches_after_redaction": 0,
        "redacted_file_count": len(redactions),
        "D5_completion_state": generated["metadata/archive-status.json"][
            "completion_state"
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVES)
    parser.add_argument(
        "--model-env", type=Path, default=Path("/mnt/d/uclaude/model.env")
    )
    parser.add_argument("--status", type=Path)
    parser.add_argument("--ci-evidence", action="append", type=Path, default=[])
    parser.add_argument("--deployment-evidence", action="append", type=Path, default=[])
    args = parser.parse_args()
    try:
        result = archive(
            args.root,
            args.archive_root,
            model_env=args.model_env,
            status=args.status,
            ci=args.ci_evidence,
            deployment=args.deployment_evidence,
        )
    except ArchiveRejected as exc:
        print(
            json.dumps(
                {
                    "archive_created_successfully": False,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            )
        )
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
        yaml.YAMLError,
    ) as exc:
        # No exception text: parsers/filesystem errors can embed credential data.
        print(
            json.dumps(
                {
                    "archive_created_successfully": False,
                    "error_type": type(exc).__name__,
                }
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
