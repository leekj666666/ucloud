"""Build isolated D5 diagnostics and the persistent 100-attempt budget relay."""

import json
import subprocess
from pathlib import Path

import httpx
from cloudsre.model_budget import ModelBudget
from cloudsre.reporting import versions
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "infra/.generated/d5"
WORK = ROOT / "evaluation/d5-runtime"


def main():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    budget_dir = WORK / "budget-private"
    budget_dir.mkdir(exist_ok=True)
    budget_dir.chmod(0o700)
    budget = ModelBudget(budget_dir / "batch.sqlite", 100)
    model = dotenv_values("/mnt/d/uclaude/model.env")
    env = {k: model[k] for k in ("OPENAI_BASE_URL", "DEEPSEEK_API_KEY", "MODEL_NAME")}
    env.update(D5_BUDGET_DATABASE="/budget/batch.sqlite", D5_BUDGET_LIMIT="100")
    file = PRIVATE / "proxy.env"
    file.write_text("".join(k + "=" + v + "\n" for k, v in env.items()))
    file.chmod(0o600)
    (ROOT / "deploy/d5-build-manifest.json").write_text(
        json.dumps(versions(ROOT), indent=2)
    )
    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "deploy/Dockerfile.d5",
            "-t",
            "cloudsre-agent:d5",
            ".",
        ],
        cwd=ROOT,
        check=True,
    )
    # No budget file is removed when the relay is restarted/replaced.
    subprocess.run(
        ["docker", "rm", "-f", "cloudsre-d5-budget-proxy"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "cloudsre-d5-budget-proxy",
            "--network",
            "sre-infra_default",
            "--memory",
            "192m",
            "--user",
            "1000:1000",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=32m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--env-file",
            str(file),
            "-v",
            str(budget_dir) + ":/budget:rw",
            "-p",
            "127.0.0.1:18090:8080",
            "cloudsre-agent:d5",
            "uvicorn",
            "cloudsre.model_budget:configured_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--no-access-log",
        ],
        check=True,
        capture_output=True,
    )
    import time

    for _ in range(30):
        try:
            health = httpx.get("http://localhost:18090/health", timeout=3).json()
            if health["limit"] == 100:
                break
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError("D5 budget relay did not become ready")
    result = {
        "image_id": subprocess.check_output(
            ["docker", "inspect", "cloudsre-agent:d5", "--format", "{{.Id}}"], text=True
        ).strip(),
        "limit": 100,
        "used_slots": budget.summary()["used_slots"],
        "real_key_only_in_d5_proxy": True,
        "ground_truth_mounted": False,
    }
    (WORK / "deployment.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
