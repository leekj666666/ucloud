"""Enable denial observation at a checkpoint; prove a denial costs no model call."""

import hashlib
import json
import subprocess
import time

import httpx
from cloudsre.durable import now
from cloudsre.model_budget import ModelBudget
from dotenv import dotenv_values

from .live import ROOT, WORK, exclusive
from .proxy_observer import export_summary, safe_summary


def main():
    with exclusive():
        budget = ModelBudget(WORK / "budget-private/batch.sqlite", 100)
        before = budget.summary()
        if any(row["state"] == "reserved_unknown" for row in before["attempts"]):
            raise RuntimeError(
                "An upstream request may be in flight; replacement refused"
            )
        running = subprocess.check_output(
            ["docker", "ps", "--filter", "name=cloudsre-d5-diag-", "-q"], text=True
        ).strip()
        if running:
            raise RuntimeError("A diagnostic container is running")
        image = subprocess.check_output(
            ["docker", "inspect", "cloudsre-d5-budget-proxy", "--format", "{{.Image}}"],
            text=True,
        ).strip()
        source = ROOT / "benchmarks/d5/proxy_observer.py"
        subprocess.run(
            ["docker", "rm", "-f", "cloudsre-d5-budget-proxy"],
            check=True,
            capture_output=True,
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
                str(ROOT / "infra/.generated/d5/proxy.env"),
                "-v",
                str(WORK / "budget-private") + ":/budget:rw",
                "-v",
                str(source) + ":/observer/proxy_observer.py:ro",
                "-p",
                "127.0.0.1:18090:8080",
                image,
                "uvicorn",
                "--app-dir",
                "/observer",
                "proxy_observer:configured_app",
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
        for _ in range(30):
            try:
                health = httpx.get("http://localhost:18090/health", timeout=3).json()
                if (
                    health.get("limit") == 100
                    and health.get("used_slots") == before["used_slots"]
                ):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Observed proxy did not become healthy")
        with budget.connection() as conn:
            candidates = conn.execute(
                "SELECT runs.id FROM runs JOIN attempts ON runs.id=attempts.run_id GROUP BY runs.id HAVING count(*) >= max(runs.max_attempts)"
            ).fetchall()
        selected = next(
            (
                row["id"]
                for row in candidates
                if (ROOT / "infra/.generated/d5" / (row["id"] + ".env")).exists()
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(
                "No already-exhausted real run is available for a safe denial probe"
            )
        environment = dotenv_values(ROOT / "infra/.generated/d5" / (selected + ".env"))
        observer_path = WORK / "budget-private/proxy-denials.sqlite"
        observed_before = safe_summary(observer_path)["observed_local_budget_denials"]
        response = httpx.post(
            "http://localhost:18090/v1/chat/completions",
            headers={"Authorization": "Bearer " + environment["CLOUDSRE_RUN_TOKEN"]},
            json={
                "model": environment["MODEL_NAME"],
                "messages": [
                    {
                        "role": "user",
                        "content": "No-dispatch observer verification using an already-exhausted run",
                    }
                ],
                "max_tokens": 8192,
            },
            timeout=10,
        )
        after = budget.summary()
        observed = export_summary(observer_path, WORK / "proxy-denials-summary.json")
        passed = (
            response.status_code == 429
            and response.json().get("error", {}).get("code") == "model_budget_exhausted"
            and before["used_slots"] == after["used_slots"]
            and observed["observed_local_budget_denials"] == observed_before + 1
        )
        result = {
            "enabled_at": now(),
            "image_id": image,
            "observer_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "first_batch_used_before": before["used_slots"],
            "first_batch_used_after": after["used_slots"],
            "probe_run_id": selected,
            "probe_http_status": response.status_code,
            "observed_denial_delta": observed["observed_local_budget_denials"]
            - observed_before,
            "historical_denials_total": None,
            "new_upstream_attempts": after["used_slots"] - before["used_slots"],
            "passed": passed,
        }
        (WORK / "observer-deployment.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
        if not passed:
            raise RuntimeError("No-dispatch denial observation verification failed")


if __name__ == "__main__":
    main()
