"""Two independent namespaces must investigate and verify with overlapping windows."""

import concurrent.futures
import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from cloudsre.durable import digest, now
from verify_d3_scenarios import incidents, runs

ROOT = Path(__file__).resolve().parents[1]
KUBE = [str(Path.home() / "sre-tools/bin/kubectl"), "--context", "kind-sre-dev"]


def main():
    folder = ROOT / "evaluation/runs" / ("d3-parallel-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    report = {"passed": False, "started_at": now(), "evaluator_cleanup_used": False}
    py = str(ROOT / ".venv/bin/python")
    original = {}
    injected = False
    try:

        def baseline(namespace):
            out = folder / (namespace + "-baseline")
            env = {
                **os.environ,
                "PYTHONPATH": "backend",
                "CLOUDSRE_NAMESPACE": namespace,
                "CLOUDSRE_MONITOR_JOB": "demo-b"
                if namespace.endswith("-b")
                else "demo",
                "CLOUDSRE_VERIFIER_URL": "http://localhost:18086"
                if namespace.endswith("-b")
                else "http://localhost:18084",
            }
            # Readiness is a separate, recorded precondition. The formal 120-second
            # window still records every request and never drops warm-up failures.
            code = """
import json, sys, time
from pathlib import Path
from cloudsre.quality import run_window, request, monitor, alert_keys, now
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
attempts = []
deadline = time.monotonic() + 120
ready = False
while time.monotonic() < deadline:
    row = {'at': now()}
    try:
        business = request('/verify')
        metrics = monitor()
        ready = business.get('passed') is True and not alert_keys(metrics)
        row['ready'] = ready
    except Exception as exc:
        row['error_type'] = type(exc).__name__
    attempts.append(row)
    (out / 'readiness.json').write_text(json.dumps(attempts, indent=2))
    if ready:
        break
    time.sleep(2)
assert ready, 'Business or monitoring not ready; formal baseline not started'
r = run_window(out)
assert r['passed'], r['checks']
"""
            subprocess.run([py, "-c", code, str(out)], cwd=ROOT, env=env, check=True)
            return json.loads((out / "result.json").read_text())

        namespaces = ("sre-demo", "sre-demo-b")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            qualities = dict(
                zip(namespaces, pool.map(baseline, namespaces), strict=True)
            )
        resources = {}
        for namespace in namespaces:
            for kind in ("Deployment", "Service"):
                for name in ("gateway", "order", "inventory"):
                    raw = json.loads(
                        subprocess.check_output(
                            KUBE + ["-n", namespace, "get", kind, name, "-o", "json"],
                            text=True,
                        )
                    )
                    resources[namespace + "/" + kind + "/" + name] = raw
            original[namespace] = resources[namespace + "/Deployment/inventory"]
        trusted = {
            "healthy_verified": True,
            "recorded_at": now(),
            "verification_sha256": digest(qualities),
            "quality": qualities["sre-demo"],
            "quality_by_namespace": qualities,
            "resources": resources,
        }
        (ROOT / "infra/.generated/d3/baseline.json").write_text(
            json.dumps(trusted, indent=2)
        )
        env = {
            **os.environ,
            "PYTHONPATH": "backend",
            "CLOUDSRE_NAMESPACE": "sre-demo",
            "CLOUDSRE_INCIDENT_NOT_BEFORE": now(),
            "CLOUDSRE_VERIFIER_B_URL": "http://cloudsre-verifier-b:8080",
        }
        subprocess.run(
            [py, "scripts/deploy_d3_runtime.py"], cwd=ROOT, env=env, check=True
        )
        previous = {r["id"] for r in incidents()}
        for namespace in namespaces:
            subprocess.run(
                KUBE
                + ["-n", namespace, "scale", "deployment/inventory", "--replicas=0"],
                check=True,
            )
        injected = True
        chosen = {}
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            for row in incidents():
                if row["id"] in previous:
                    continue
                records = runs(row["id"])
                if not records:
                    continue
                candidate = records[-1]
                payload = candidate["payload"]
                ns = payload.get("namespace")
                if ns in namespaces and (
                    ns not in chosen or chosen[ns]["id"] == candidate["id"]
                ):
                    chosen[ns] = candidate
            if len(chosen) == 2 and all(
                r["phase"] in {"RESOLVED", "ESCALATED"} for r in chosen.values()
            ):
                break
            time.sleep(1)
        report["workflows"] = chosen
        assert len(chosen) == 2 and all(
            r["phase"] == "RESOLVED" for r in chosen.values()
        )
        windows = [r["payload"]["quality"] for r in chosen.values()]
        assert all(
            w["passed"]
            and w["duration_seconds"] >= 120
            and w["load_requests"] >= 120
            and w["business_checks"] >= 12
            for w in windows
        )
        overlap = (
            min(datetime.fromisoformat(w["ended_at"]) for w in windows)
            - max(datetime.fromisoformat(w["started_at"]) for w in windows)
        ).total_seconds()
        assert overlap > 0, "Verification windows did not overlap"
        assert len({r["payload"]["lease"]["resource"] for r in chosen.values()}) == 2
        report.update(passed=True, verification_overlap_seconds=overlap)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:600]
        raise
    finally:
        subprocess.run(
            ["docker", "stop", "cloudsre-worker-d3-1", "cloudsre-worker-d3-2"],
            capture_output=True,
            check=True,
        )
        if injected and not report["passed"]:
            for namespace, before in original.items():
                current = json.loads(
                    subprocess.check_output(
                        KUBE
                        + [
                            "-n",
                            namespace,
                            "get",
                            "deployment",
                            "inventory",
                            "-o",
                            "json",
                        ],
                        text=True,
                    )
                )
                patch = [
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": current["metadata"]["resourceVersion"],
                    },
                    {"op": "replace", "path": "/spec", "value": before["spec"]},
                ]
                subprocess.run(
                    KUBE
                    + [
                        "-n",
                        namespace,
                        "patch",
                        "deployment",
                        "inventory",
                        "--type=json",
                        "-p",
                        json.dumps(patch),
                    ],
                    check=True,
                )
            report["evaluator_cleanup_used"] = True
        report["ended_at"] = now()
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        print(
            json.dumps({"run_id": folder.name, "passed": report["passed"]}), flush=True
        )


if __name__ == "__main__":
    main()
