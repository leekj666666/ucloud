"""Evaluator-owned fault setup/cleanup; never mounted into an Agent container."""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx
from cloudsre.durable import now
from verify_d3_scenarios import command

ALLOWED_ROOT = Path("/mnt/d/uclaude/d4-browser-runs").resolve()


def field_value(raw, pointer):
    value = raw
    for part in pointer.strip("/").split("/"):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["inject", "cleanup", "inspect"])
    parser.add_argument(
        "--case", choices=["config", "route", "dependency"], default="config"
    )
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    folder = Path(args.run_dir).resolve()
    if not folder.is_relative_to(ALLOWED_ROOT) or not folder.name.startswith("d4-ui-"):
        raise ValueError("Fixture output outside the explicit evaluation root")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "fixture.json"
    if args.operation == "inject":
        assert not path.exists(), "Fixture identity is immutable"
        preflight = []
        clear_since = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            healthy = httpx.post("http://localhost:18084/verify", timeout=35).json()
            alerts = httpx.get(
                "http://localhost:19090/api/v1/query",
                params={
                    "query": 'ALERTS{alertstate="firing",namespace="sre-demo",severity=~"warning|critical|error"}'
                },
                timeout=8,
            ).json()["data"]["result"]
            preflight.append(
                {
                    "at": now(),
                    "business_passed": healthy.get("passed"),
                    "alerts": alerts,
                }
            )
            (folder / "preflight.json").write_text(json.dumps(preflight, indent=2))
            if healthy.get("passed") and not alerts:
                clear_since = clear_since or time.monotonic()
                if time.monotonic() - clear_since >= 15:
                    break
            else:
                clear_since = None
            time.sleep(2)
        else:
            raise RuntimeError(
                "Precondition business or alerts did not settle; no fault injected"
            )
        target = (
            ("service", "inventory")
            if args.case == "route"
            else ("deployment", "order" if args.case == "config" else "inventory")
        )
        before = json.loads(command("get", *target, "-o", "json"))
        if args.case == "config":
            containers = before["spec"]["template"]["spec"]["containers"]
            ci = next(i for i, c in enumerate(containers) if c["name"] == "order")
            ei = next(
                i
                for i, e in enumerate(containers[ci]["env"])
                if e["name"] == "INVENTORY_URL"
            )
            pointer = f"/spec/template/spec/containers/{ci}/env/{ei}/value"
            injected_value = "http://inventory-" + uuid.uuid4().hex[:8] + ":8000"
        elif args.case == "route":
            pointer = "/spec/ports/0/targetPort"
            injected_value = 8011
        else:
            pointer = "/spec/replicas"
            injected_value = 0
        record = {
            "case": args.case,
            "target": list(target),
            "before": before,
            "field": pointer,
            "injected_value": injected_value,
            "fault_at": now(),
            "evaluator_owned": True,
            "agent_inputs_contain_fixture": False,
        }
        path.write_text(json.dumps(record, indent=2))
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": before["metadata"]["resourceVersion"],
            },
            {"op": "replace", "path": pointer, "value": injected_value},
        ]
        command("patch", *target, "--type=json", "-p", json.dumps(patch))
        if target[0] == "deployment" and args.case == "config":
            command("rollout", "status", "deployment/order", "--timeout=120s")
        deadline = time.monotonic() + 100
        symptoms = []
        observed_alerts = []
        while time.monotonic() < deadline:
            result = httpx.post("http://localhost:18084/sample", timeout=35).json()
            symptoms.append({"at": now(), "result": result})
            response = httpx.get(
                "http://localhost:19090/api/v1/query",
                params={
                    "query": 'ALERTS{alertstate="firing",namespace="sre-demo",severity=~"warning|critical|error"}'
                },
                timeout=8,
            )
            observed_alerts = response.json().get("data", {}).get("result", [])
            if not result.get("passed") and observed_alerts:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Real fault symptoms and alert were not observed")
        record.update(
            fault_confirmed=True, symptoms=symptoms, observed_alerts=observed_alerts
        )
        path.write_text(json.dumps(record, indent=2))
        print(
            json.dumps(
                {"injected": True, "real_alert_observed": True, "fixture": folder.name}
            )
        )
        return
    record = json.loads(path.read_text())
    current = json.loads(command("get", *record["target"], "-o", "json"))
    assert current["metadata"]["uid"] == record["before"]["metadata"]["uid"]
    observed = field_value(current, record["field"])
    healthy_value = field_value(record["before"], record["field"])
    if args.operation == "inspect":
        print(
            json.dumps(
                {
                    "matches_injected": observed == record["injected_value"],
                    "matches_healthy": observed == healthy_value,
                    "resource_version": current["metadata"]["resourceVersion"],
                }
            )
        )
        return
    used = False
    if observed != healthy_value:
        assert observed == record["injected_value"], (
            "Foreign value prevents evaluator cleanup"
        )
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": record["field"], "value": observed},
            {"op": "replace", "path": record["field"], "value": healthy_value},
        ]
        command("patch", *record["target"], "--type=json", "-p", json.dumps(patch))
        used = True
        if record["target"][0] == "deployment":
            command(
                "rollout",
                "status",
                "deployment/" + record["target"][1],
                "--timeout=120s",
            )
    record["evaluator_cleanup_used"] = used
    record["cleanup_at"] = now()
    path.write_text(json.dumps(record, indent=2))
    print(json.dumps({"evaluator_cleanup_used": used}))


if __name__ == "__main__":
    main()
