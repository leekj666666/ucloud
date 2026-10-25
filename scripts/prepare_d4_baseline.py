"""Independent healthy baselines for both console business graphs."""

import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cloudsre.durable import digest, now

ROOT = Path(__file__).resolve().parents[1]
KUBE = [str(Path.home() / "sre-tools/bin/kubectl"), "--context", "kind-sre-dev"]


def main():
    folder = ROOT / "evaluation/runs" / ("d4-baseline-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    namespaces = ("sre-demo", "sre-demo-b")

    def baseline(namespace):
        target = folder / namespace
        env = {
            **os.environ,
            "PYTHONPATH": "backend",
            "CLOUDSRE_NAMESPACE": namespace,
            "CLOUDSRE_MONITOR_JOB": "demo-b" if namespace.endswith("-b") else "demo",
            "CLOUDSRE_VERIFIER_URL": "http://localhost:18086"
            if namespace.endswith("-b")
            else "http://localhost:18084",
        }
        code = """
import sys,time
from cloudsre.quality import request,monitor,alert_keys,run_window
deadline=time.monotonic()+150
while True:
 try:
  assert request('/verify')['passed']
  assert not alert_keys(monitor())
  break
 except Exception:
  if time.monotonic()>deadline:raise
  time.sleep(2)
result=run_window(sys.argv[1])
assert result['passed'],result['checks']
"""
        subprocess.run(
            [str(ROOT / ".venv/bin/python"), "-c", code, str(target)],
            cwd=ROOT,
            env=env,
            check=True,
        )
        return json.loads((target / "result.json").read_text())

    with ThreadPoolExecutor(max_workers=2) as pool:
        qualities = dict(zip(namespaces, pool.map(baseline, namespaces), strict=True))
    resources = {}
    for namespace in namespaces:
        for kind in ("Deployment", "Service"):
            for name in ("gateway", "order", "inventory"):
                resources[namespace + "/" + kind + "/" + name] = json.loads(
                    subprocess.check_output(
                        KUBE + ["-n", namespace, "get", kind, name, "-o", "json"],
                        text=True,
                    )
                )
    document = {
        "healthy_verified": True,
        "recorded_at": now(),
        "verification_sha256": digest(qualities),
        "quality": qualities["sre-demo"],
        "quality_by_namespace": qualities,
        "resources": resources,
    }
    path = ROOT / "infra/.generated/d4/baseline.json"
    path.write_text(json.dumps(document, indent=2))
    path.chmod(0o644)
    print(
        json.dumps(
            {"folder": str(folder), "passed": True, "namespaces": list(qualities)}
        )
    )


if __name__ == "__main__":
    main()
