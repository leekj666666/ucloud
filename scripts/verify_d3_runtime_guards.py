"""Real diagnostic process timeout, RBAC denial and database session interruption."""

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

from cloudsre.durable import lock_key, now
from cloudsre.quality import alert_keys, monitor, request
from cloudsre.worker import database_url
from sqlalchemy import create_engine, text
from verify_d3_scenarios import command, incidents, runs

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["database_disconnect", "execution_denied", "diagnostic_timeout"],
        required=True,
    )
    args = parser.parse_args()
    folder = (
        ROOT / "evaluation/runs" / ("d3-" + args.mode + "-" + uuid.uuid4().hex[:12])
    )
    folder.mkdir(parents=True)
    report = {
        "mode": args.mode,
        "passed": False,
        "started_at": now(),
        "evaluator_cleanup_used": False,
    }
    original = json.loads(command("get", "deployment", "inventory", "-o", "json"))
    role = json.loads(command("get", "role", "cloudsre-execute", "-o", "json"))
    injected = changed_role = False
    try:
        assert request("/verify")["passed"]
        deadline = time.monotonic() + 150
        while alert_keys(monitor()):
            assert time.monotonic() < deadline
            time.sleep(2)
        previous = {row["id"] for row in incidents()}
        command("scale", "deployment/inventory", "--replicas=0")
        injected = True
        chosen = None
        disturbed = False
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if chosen is None:
                for row in incidents():
                    if row["id"] not in previous:
                        records = runs(row["id"])
                        if (
                            records
                            and records[-1]["payload"].get("schema_version") == 3
                        ):
                            chosen = row
                            break
            if chosen:
                latest = runs(chosen["id"])[-1]
                payload = latest["payload"]
                marker = (
                    ROOT
                    / "evaluation/d3-runtime/state"
                    / ("barrier-" + chosen["id"] + "-before_write.json")
                )
                if (
                    args.mode != "diagnostic_timeout"
                    and not disturbed
                    and marker.exists()
                ):
                    report["epoch_before"] = payload["lease"]["epoch"]
                    if args.mode == "execution_denied":
                        denied = [{**rule, "verbs": ["get"]} for rule in role["rules"]]
                        command(
                            "patch",
                            "role",
                            "cloudsre-execute",
                            "--type=merge",
                            "-p",
                            json.dumps({"rules": denied}),
                        )
                        changed_role = True
                    key = lock_key("incident:" + chosen["id"]) & ((1 << 64) - 1)
                    engine = create_engine(database_url())
                    try:
                        with engine.connect().execution_options(
                            isolation_level="AUTOCOMMIT"
                        ) as connection:
                            pids = (
                                connection.execute(
                                    text(
                                        "SELECT DISTINCT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype='advisory' AND l.granted AND l.classid=:hi AND l.objid=:lo AND a.usename='sre_worker'"
                                    ),
                                    {"hi": key >> 32, "lo": key & 0xFFFFFFFF},
                                )
                                .scalars()
                                .all()
                            )
                            assert len(pids) == 1
                            assert connection.execute(
                                text("SELECT pg_terminate_backend(:pid)"),
                                {"pid": pids[0]},
                            ).scalar()
                    finally:
                        engine.dispose()
                    disturbed = True
                    report["interrupted_at"] = now()
                if latest["phase"] in {"RESOLVED", "ESCALATED"}:
                    break
            time.sleep(0.3)
        assert chosen is not None
        report["journal"] = latest["payload"]
        journal = latest["payload"]
        state = ROOT / "evaluation/d3-runtime/state" / latest["id"]
        ledger = state / "external-actions.jsonl"
        writes = (
            [json.loads(line) for line in ledger.read_text().splitlines()]
            if ledger.exists()
            else []
        )
        report["external_requests"] = writes
        if args.mode == "database_disconnect":
            assert disturbed and latest["phase"] == "RESOLVED"
            assert journal["lease"]["epoch"] > report["epoch_before"]
            assert (
                journal["quality"]["passed"]
                and journal["quality"]["duration_seconds"] >= 120
            )
            assert sum(x["event"] == "request_started" for x in writes) == 1
        else:
            assert (
                latest["phase"] == "ESCALATED" and journal["ticket"]["status"] == "OPEN"
            )
            current = json.loads(
                command("get", "deployment", "inventory", "-o", "json")
            )
            assert current["spec"]["replicas"] == 0
            if args.mode == "diagnostic_timeout":
                assert len(journal["diagnoses"]) == 2 and all(
                    x["state"] == "timeout" for x in journal["diagnoses"]
                )
                assert not writes
            else:
                assert disturbed and journal["action_result"]["state"] == "REJECTED"
        report["passed"] = True
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
        if changed_role:
            command(
                "patch",
                "role",
                "cloudsre-execute",
                "--type=merge",
                "-p",
                json.dumps({"rules": role["rules"]}),
            )
        if injected and (not report["passed"] or args.mode != "database_disconnect"):
            current = json.loads(
                command("get", "deployment", "inventory", "-o", "json")
            )
            command(
                "patch",
                "deployment",
                "inventory",
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": current["metadata"]["resourceVersion"],
                        },
                        {"op": "replace", "path": "/spec", "value": original["spec"]},
                    ]
                ),
            )
            command("rollout", "status", "deployment/inventory", "--timeout=120s")
            report["evaluator_cleanup_used"] = True
        report["ended_at"] = now()
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        print(
            json.dumps(
                {"run_id": folder.name, "passed": report["passed"], "mode": args.mode}
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
