"""Check the reviewable Git source set against actual local runtime credentials."""

import json
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    secrets = set()
    files = [
        Path("/mnt/d/uclaude/model.env"),
        ROOT / "infra/.env",
        ROOT / "infra/.env.demo",
    ]
    files.extend((ROOT / "infra/.generated").rglob("*.env"))
    for path in files:
        if path.exists():
            for key, value in dotenv_values(path).items():
                if (
                    value
                    and len(value) >= 8
                    and any(
                        part in key.upper()
                        for part in ("KEY", "TOKEN", "PASSWORD", "DATABASE_URL")
                    )
                ):
                    secrets.add(value.encode())
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).split(b"\0")
    paths = [ROOT / name.decode() for name in names if name]
    matches = [
        str(path.relative_to(ROOT))
        for path in paths
        if any(secret in path.read_bytes() for secret in secrets)
    ]
    result = {
        "passed": not matches,
        "source_files_scanned": len(paths),
        "known_credential_matches": matches,
        "real_values_printed": False,
    }
    target = ROOT / "evaluation/d5-runtime/source-secret-audit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    if matches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
