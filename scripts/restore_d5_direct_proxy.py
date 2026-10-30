import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

R = Path("/home/lee/sre-project")
W = R / "evaluation/d5-runtime"
P = W / "budget-private"
sys.path[:0] = [str(R), str(R / "backend")]
from cloudsre.model_budget import ModelBudget

from benchmarks.d5.live import exclusive

with exclusive():
    backup = "cloudsre-d5-budget-proxy-before-direct"
    saved = subprocess.run(
        ["docker", "inspect", backup], capture_output=True, check=False
    )
    recovering = saved.returncode == 0
    if recovering:
        try:
            with httpx.Client(trust_env=False) as c:
                response = c.get("http://127.0.0.1:18090/health", timeout=3)
                response.raise_for_status()
            print(
                json.dumps(
                    {"status": "existing_direct_proxy_healthy", "changed": False}
                )
            )
            sys.exit(0)
        except httpx.HTTPError:
            pass
    info = json.loads(
        saved.stdout
        if recovering
        else subprocess.check_output(["docker", "inspect", "cloudsre-d5-budget-proxy"])
    )[0]
    assert not subprocess.check_output(
        ["docker", "ps", "--filter", "name=cloudsre-d5-diag-", "-q"]
    ).strip()
    assert all(
        a["state"] == "finished"
        for a in ModelBudget(P / "batch3.sqlite", 100).summary()["attempts"]
    )
    env = os.environ.copy()
    for k in list(env):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            del env[k]
    for item in info["Config"]["Env"]:
        k, _, v = item.partition("=")
        if k in (
            "OPENAI_BASE_URL",
            "DEEPSEEK_API_KEY",
            "MODEL_NAME",
            "D5_BUDGET_LIMIT",
        ):
            env[k] = v
    env["D5_BUDGET_DATABASE"] = str(P / "batch3.sqlite")
    env["PYTHONPATH"] = str(R / "backend")
    ip = subprocess.check_output(["hostname", "-I"], text=True).split()[0]
    log_path = P / "direct-proxy.log"
    log_path.touch(mode=0o600, exist_ok=True)
    os.chmod(log_path, 0o600)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            [
                str(R / ".venv/bin/python"),
                "-m",
                "uvicorn",
                "--app-dir",
                str(R / "benchmarks/d5"),
                "proxy_observer:configured_app",
                "--factory",
                "--host",
                ip,
                "--port",
                "18091",
                "--no-access-log",
            ],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=R,
        )
    try:
        for _ in range(30):
            try:
                with httpx.Client(trust_env=False) as c:
                    health = c.get(f"http://{ip}:18091/health", timeout=2).json()
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(0.3)
        else:
            raise RuntimeError("Host proxy did not become healthy")
        # Verify container-to-host reachability BEFORE replacing the old proxy.
        code = f'import urllib.request; print(urllib.request.urlopen("http://{ip}:18091/health",timeout=5).status)'
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "sre-infra_default",
                info["Image"],
                "python",
                "-c",
                code,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode or result.stdout.strip() != "200":
            raise RuntimeError(
                "Container cannot reach WSL direct proxy; old deployment retained"
            )
        relay = R / "benchmarks/d5/direct_tcp_relay.py"
        assert relay.is_file(), "Packaged TCP relay is missing"
        # Preserve the prior container, stopped, for rollback.
        if recovering:
            subprocess.run(
                ["docker", "rm", "-f", "cloudsre-d5-budget-proxy"],
                capture_output=True,
                check=False,
            )
        else:
            subprocess.run(
                ["docker", "rename", "cloudsre-d5-budget-proxy", backup],
                check=True,
                capture_output=True,
            )
            subprocess.run(["docker", "stop", backup], check=True, capture_output=True)
        try:
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
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "-e",
                    "DIRECT_HOST=" + ip,
                    "-v",
                    str(relay) + ":/relay.py:ro",
                    "-p",
                    "127.0.0.1:18090:8080",
                    info["Image"],
                    "python",
                    "/relay.py",
                ],
                check=True,
                capture_output=True,
            )
            time.sleep(1)
            with httpx.Client(trust_env=False) as c:
                after = c.get("http://127.0.0.1:18090/health", timeout=10).json()
            assert after == health
        except Exception:
            subprocess.run(
                ["docker", "rm", "-f", "cloudsre-d5-budget-proxy"],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["docker", "rename", backup, "cloudsre-d5-budget-proxy"], check=True
            )
            subprocess.run(
                ["docker", "start", "cloudsre-d5-budget-proxy"],
                check=True,
                capture_output=True,
            )
            raise
        proof = {
            "host_pid": proc.pid,
            "host_ip": ip,
            "port": 18091,
            "upstream": "https://api.deepseek.com",
            "trust_env_effect": "proxy variables removed from host service",
            "backup_container": backup,
            "ledger_preserved": True,
            "thinking": "original_provider_default_enabled",
            "automatic_retries": 0,
            "restart_persistence": False,
        }
        (W / "direct-proxy-deployment.json").write_text(json.dumps(proof, indent=2))
        print(json.dumps(proof))
    except Exception:
        proc.terminate()
        raise
