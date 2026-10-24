"""Independent healthy baseline; fault manifests and answers are not diagnostic inputs."""

import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

from cloudsre.durable import now
from cloudsre.kube import PREFIX
from cloudsre.quality import alert_keys, monitor, request, run_window

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "evaluation/runs" / ("d3-baseline-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=True)
    assert request("/verify")["passed"]
    end = time.monotonic() + 150
    while alert_keys(monitor()):
        if time.monotonic() > end:
            raise RuntimeError("Baseline still has related firing alerts")
        time.sleep(2)
    quality = run_window(folder)
    assert quality["passed"], quality["checks"]
    resources = {}
    for kind in ("Deployment", "Service"):
        for name in ("gateway", "order", "inventory"):
            raw = json.loads(
                subprocess.check_output(
                    PREFIX + ["get", kind, name, "-o", "json"], text=True
                )
            )
            resources["sre-demo/" + kind + "/" + name] = raw
    baseline = {
        "healthy_verified": True,
        "recorded_at": now(),
        "verification_sha256": hashlib.sha256(
            (folder / "result.json").read_bytes()
        ).hexdigest(),
        "quality": quality,
        "resources": resources,
    }
    p = ROOT / "infra/.generated/d3/baseline.json"
    p.write_text(json.dumps(baseline, indent=2))
    p.chmod(0o644)
    print(
        json.dumps({"baseline": str(p), "passed": True, "p95": quality["p95_seconds"]})
    )


if __name__ == "__main__":
    main()
