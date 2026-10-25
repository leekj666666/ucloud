"""Prove actual diagnostic image/mounts exclude answers and real runtime keys."""

import json
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    checks = {}
    info = json.loads(
        subprocess.check_output(["docker", "inspect", "cloudsre-agent:d5"], text=True)
    )[0]
    keys = {value.split("=", 1)[0] for value in info["Config"]["Env"]}
    checks["real_keys_not_baked_into_image"] = (
        not {
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "CLOUDSRE_DIAG_TOKEN",
            "DATABASE_URL",
        }
        & keys
    )
    code = """from pathlib import Path
import os
for name in ('/app/benchmarks','/app/evaluation','/app/scripts','/root/.kube/config','/var/run/docker.sock','/app/infra/.generated'):
 try:
  assert not Path(name).exists(),name
 except PermissionError:
  assert name.startswith('/root/'),name
assert not os.environ.get('DEEPSEEK_API_KEY')
print('isolated')
"""
    done = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=16m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "/app/.venv/bin/python",
            "cloudsre-agent:d5",
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    checks["image_has_no_evaluator_scripts_answers_admin_or_key"] = done.returncode == 0
    secrets = []
    for file in (
        Path("/mnt/d/uclaude/model.env"),
        ROOT / "infra/.env",
        ROOT / "infra/.env.demo",
    ):
        secrets += [
            value.encode()
            for key, value in dotenv_values(file).items()
            if value
            and len(value) >= 8
            and any(
                word in key.upper()
                for word in ("API_KEY", "PASSWORD", "TOKEN", "DATABASE_URL")
            )
        ]
    files = list((ROOT / "evaluation/d5-runtime/snapshots").rglob("*.json"))
    matches = [
        str(file.relative_to(ROOT))
        for file in files
        if any(value in file.read_bytes() for value in secrets)
    ]
    checks["snapshot_files_contain_no_known_runtime_credentials"] = not matches
    checks["snapshots_exist"] = bool(files)
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "snapshot_files_scanned": len(files),
        "known_secret_matches": matches,
        "model_calls": 0,
        "image_inspection_id": info["Id"],
        "image_probe_error": done.stderr[-1000:] if done.returncode else None,
    }
    (ROOT / "evaluation/d5-isolation.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    assert result["passed"]


if __name__ == "__main__":
    main()
