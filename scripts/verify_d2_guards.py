"""Live API-server race checks. No model and no business URL change."""

import json
import subprocess
import uuid
from pathlib import Path

from cloudsre import kube


def command(*args):
    return subprocess.check_output(kube.PREFIX + list(args), text=True)


def main():
    output = {"checks": {}, "model_calls": 0}
    original_read = kube.read
    before = kube.deployment_snapshot(original_read("deployment", "order"))
    old = before["env"]["INVENTORY_URL"]
    try:
        command(
            "set", "env", "deployment/order", "CONFIG_REVISION=" + uuid.uuid4().hex[:8]
        )
        changed = kube.deployment_snapshot(original_read("deployment", "order"))
        try:
            kube.guarded_patch(before, old, old, "guard-precondition")
        except ValueError:
            pass
        else:
            raise AssertionError("Changed spec was accepted")
        after = kube.deployment_snapshot(original_read("deployment", "order"))
        assert (
            changed["generation"] == after["generation"]
            and changed["spec_hash"] == after["spec_hash"]
        )
        output["checks"]["policy_to_execution_spec_change_rejected_without_write"] = (
            True
        )
    finally:
        command("set", "env", "deployment/order", "CONFIG_REVISION-")
        command("rollout", "status", "deployment/order", "--timeout=120s")
    approved = kube.deployment_snapshot(original_read("deployment", "order"))
    fired = False
    intervening = None

    def race(resource, name=None):
        nonlocal fired, intervening
        raw = original_read(resource, name)
        if not fired:
            fired = True
            command(
                "annotate",
                "deployment/order",
                "cloudsre/concurrency-check=" + uuid.uuid4().hex,
                "--overwrite",
            )
            intervening = kube.deployment_snapshot(original_read("deployment", "order"))
        return raw

    try:
        kube.read = race
        try:
            kube.guarded_patch(approved, old, old, "guard-atomic")
        except RuntimeError:
            pass
        else:
            raise AssertionError("API-server atomic conflict was accepted")
        after = kube.deployment_snapshot(original_read("deployment", "order"))
        assert (
            after["generation"] == intervening["generation"]
            and after["spec_hash"] == approved["spec_hash"]
        )
        output["checks"]["read_to_patch_api_conflict_rejected_without_spec_write"] = (
            True
        )
    finally:
        kube.read = original_read
        command("annotate", "deployment/order", "cloudsre/concurrency-check-")
    output["atomic_conflict_versions"] = {
        "approved": approved,
        "competing": intervening,
        "after": after,
    }
    path = Path("/mnt/d/uclaude/d2-live-guards.json")
    path.write_text(json.dumps(output, indent=2))
    print(json.dumps(output), flush=True)


if __name__ == "__main__":
    main()
