"""Real fault cases. Evaluator code is excluded from all Agent containers."""

import argparse
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
from cloudsre.durable import now
from cloudsre.kube import PREFIX
from cloudsre.quality import alert_keys, monitor, request

ROOT = Path(__file__).resolve().parents[1]
API = "http://localhost:18080"


def command(*args):
    return subprocess.check_output(PREFIX + list(args), text=True)


def incidents():
    r = httpx.get(API + "/incidents", timeout=10)
    r.raise_for_status()
    return r.json()


def runs(ident):
    r = httpx.get(API + "/incidents/" + ident + "/runs", timeout=10)
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", choices=["config", "route", "dependency", "resources"], required=True
    )
    parser.add_argument("--unrelated-release", action="store_true")
    parser.add_argument(
        "--crash-at",
        choices=[
            "before_diagnosis",
            "after_diagnosis",
            "before_write",
            "after_write",
            "verification",
        ],
    )
    args = parser.parse_args()
    folder = (
        ROOT / "evaluation/runs" / ("d3-" + args.case + "-" + uuid.uuid4().hex[:12])
    )
    folder.mkdir(parents=True)
    report = {
        "case": args.case,
        "run_id": folder.name,
        "started_at": now(),
        "checks": {},
        "evaluator_cleanup_used": False,
    }
    target = {
        "config": ("deployment", "order"),
        "route": ("service", "inventory"),
        "dependency": ("deployment", "inventory"),
        "resources": ("deployment", "inventory"),
    }[args.case]
    before = json.loads(command("get", *target, "-o", "json"))
    stop = threading.Event()
    traffic = None
    injected = False
    crashed = False
    crash_epoch = None
    try:
        assert request("/verify")["passed"]
        end = time.monotonic() + 150
        while alert_keys(monitor()):
            if time.monotonic() > end:
                raise RuntimeError("Related alerts remain before injection")
            time.sleep(2)
        previous = {r["id"] for r in incidents()}
        if args.unrelated_release:
            command(
                "set",
                "env",
                "deployment/order",
                "RELEASE_ID=r-" + uuid.uuid4().hex[:12],
            )
            command("rollout", "status", "deployment/order", "--timeout=120s")
            report["unrelated_release"] = {"resource": "order", "at": now()}
        report["fault_at"] = now()
        fault_clock = time.monotonic()
        injected = True
        if args.case == "config":
            command(
                "set",
                "env",
                "deployment/order",
                "INVENTORY_URL=http://inventory-" + uuid.uuid4().hex[:8] + ":8000",
            )
        elif args.case == "route":
            command(
                "patch",
                "service",
                "inventory",
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "replace",
                            "path": "/spec/ports/0/targetPort",
                            "value": 8011,
                        }
                    ]
                ),
            )
        elif args.case == "dependency":
            command("scale", "deployment/inventory", "--replicas=0")
        else:
            resources = {
                "requests": {"memory": "8Mi", "cpu": "20m"},
                "limits": {"memory": "8Mi", "cpu": "200m"},
            }
            command(
                "patch",
                "deployment",
                "inventory",
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "replace",
                            "path": "/spec/template/spec/containers/0/resources",
                            "value": resources,
                        }
                    ]
                ),
            )

        def load():
            with (folder / "fault-traffic.jsonl").open("w") as output:
                while not stop.is_set():
                    item = {"started_at": now()}
                    try:
                        item["result"] = request("/sample")
                    except (httpx.HTTPError, ValueError) as exc:
                        item["error_type"] = type(exc).__name__
                    item["completed_at"] = now()
                    output.write(json.dumps(item) + "\n")
                    output.flush()
                    stop.wait(1)

        # OOM rollout failure is signalled by resource monitoring while old pods serve.
        # Other cases use extra traffic only until the real incident is observed.
        (folder / "fault-traffic.jsonl").touch()
        if args.case != "resources":
            traffic = threading.Thread(target=load, daemon=True)
            traffic.start()
        print("Injected " + args.case + "; waiting for dynamic diagnosis", flush=True)
        chosen = None
        end = time.monotonic() + 120
        while time.monotonic() < end and chosen is None:
            for row in reversed(incidents()):
                if row["id"] in previous:
                    continue
                records = runs(row["id"])
                if records and records[-1]["payload"].get("schema_version") == 3:
                    chosen = row
                    break
            time.sleep(0.5)
        assert chosen is not None, "No D3 incident within 120 seconds"
        stop.set()
        if traffic:
            traffic.join(timeout=40)
            assert not traffic.is_alive()
        report["detected_seconds"] = time.monotonic() - fault_clock
        report["incident_id"] = chosen["id"]
        report["checks"]["real_alert_and_dynamic_investigation"] = True
        end = time.monotonic() + 600
        journal = None
        while time.monotonic() < end:
            found = runs(chosen["id"])[-1]
            payload = found["payload"]
            if args.crash_at and not crashed:
                state = ROOT / "evaluation/d3-runtime/state"
                marker = state / (
                    "barrier-" + chosen["id"] + "-" + args.crash_at + ".json"
                )
                samples = state / found["id"] / "recovery-1/samples.jsonl"
                ready = (
                    (samples.exists() and samples.stat().st_size > 0)
                    if args.crash_at == "verification"
                    else marker.exists()
                )
                if ready:
                    owner = payload["worker_instance"]
                    names = ["cloudsre-worker-d3-1", "cloudsre-worker-d3-2"]
                    matches = [
                        name
                        for name in names
                        if subprocess.check_output(
                            [
                                "docker",
                                "inspect",
                                "--format",
                                "{{.Config.Hostname}}",
                                name,
                            ],
                            text=True,
                        ).strip()
                        == owner
                    ]
                    assert len(matches) == 1, "Cannot identify current worker owner"
                    crash_epoch = payload["lease"]["epoch"]
                    subprocess.run(
                        ["docker", "kill", "--signal", "KILL", matches[0]],
                        check=True,
                        capture_output=True,
                    )
                    report["crash"] = {
                        "phase": args.crash_at,
                        "container": matches[0],
                        "epoch_before": crash_epoch,
                        "at": now(),
                        "workflow_id": found["id"],
                    }
                    crashed = True
            phases = [e["phase"] for e in found["payload"]["events"]]
            if "EXECUTING" in phases:
                stop.set()
            if found["phase"] in {"RESOLVED", "ESCALATED"}:
                journal = found["payload"]
                break
            time.sleep(0.5)
        assert journal is not None, "Worker deadline exceeded"
        report["journal"] = journal
        assert journal["outcome"] == "agent_restored_and_verified", journal.get("error")
        stop.set()
        if traffic:
            traffic.join(timeout=40)
            assert not traffic.is_alive()
        report["checks"]["agent_repair_without_cleanup"] = True
        quality = journal["quality"]
        assert (
            quality["passed"]
            and quality["duration_seconds"] >= 120
            and quality["business_checks"] >= 12
            and quality["load_requests"] >= 120
        )
        report["checks"]["full_quality_window"] = True
        if args.crash_at:
            assert crashed and journal["lease"]["epoch"] > crash_epoch
            assert journal["run_id"] == report["crash"]["workflow_id"]
            ledger = (
                ROOT
                / "evaluation/d3-runtime/state"
                / journal["run_id"]
                / "external-actions.jsonl"
            )
            writes = [json.loads(line) for line in ledger.read_text().splitlines()]
            assert sum(x["event"] == "request_started" for x in writes) == 1
            if args.crash_at == "after_write":
                assert journal["action_result"]["reconciled"]
                assert not journal["action_result"]["http_attempted"]
            if args.crash_at == "verification":
                assert journal["verification_attempts"] >= 2
            report["checks"]["same_run_takeover_single_http_mutation"] = True
        expected = {
            "config": "restore_env",
            "route": "restore_service_port",
            "dependency": "restore_replicas",
            "resources": "restore_resources",
        }[args.case]
        assert journal["action_intent"]["action"] == expected
        assert journal["action_intent"]["resource"].endswith("/" + target[1])
        report["checks"]["causal_target_and_bounded_action"] = True
        rows = [
            json.loads(s)
            for s in (folder / "fault-traffic.jsonl").read_text().splitlines()
        ]
        assert not rows or max(s["completed_at"] for s in rows) <= quality["started_at"]
        report["checks"]["fault_traffic_not_in_recovery_window"] = True
        final = request("/verify")
        assert final["passed"]
        report["final_business"] = final
        report["passed"] = True
    except (
        AssertionError,
        RuntimeError,
        ValueError,
        KeyError,
        OSError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        report.update(
            passed=False, error_type=type(exc).__name__, error=str(exc)[:1200]
        )
        raise
    finally:
        stop.set()
        if traffic:
            traffic.join(timeout=40)
        if injected and not report.get("passed"):
            # Stop scoped workers before evaluator cleanup; never race a repair.
            subprocess.run(
                ["docker", "stop", "cloudsre-worker-d3-1", "cloudsre-worker-d3-2"],
                capture_output=True,
                check=False,
            )
            current = json.loads(command("get", *target, "-o", "json"))
            patch = [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": current["metadata"]["resourceVersion"],
                },
                {"op": "replace", "path": "/spec", "value": before["spec"]},
            ]
            command("patch", *target, "--type=json", "-p", json.dumps(patch))
            if target[0] == "deployment":
                command(
                    "rollout", "status", "deployment/" + target[1], "--timeout=120s"
                )
            report["evaluator_cleanup_used"] = True
        if crashed:
            subprocess.run(
                ["docker", "start", report["crash"]["container"]],
                capture_output=True,
                check=True,
            )
        report["ended_at"] = now()
        (folder / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        (ROOT / "evaluation/d3-last-case.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in ("run_id", "passed", "error", "evaluator_cleanup_used")
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
