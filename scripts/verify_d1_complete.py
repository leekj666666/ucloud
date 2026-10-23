"""Strict D1 live acceptance. Evaluator restores every injected fault; no LLM."""

import concurrent.futures
import hashlib
import json
import os
import statistics
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cloudsre.demo import Order, Reservation, Stock, Tenant
from cloudsre.kube import PREFIX
from cloudsre.ports import GATEWAY_URL, business_ports
from cloudsre.release_history import collect, digest
from cloudsre.verification_data import VerificationData, verification_database_url
from dotenv import dotenv_values
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
PROM = "http://localhost:19090"
LOKI = "http://localhost:13100"
API = "http://localhost:18080"


def now():
    return datetime.now(UTC).isoformat()


def kube(*args):
    return subprocess.check_output(PREFIX + list(args), text=True)


def wait(fn, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("Acceptance condition timed out")


def prom(query):
    r = httpx.get(PROM + "/api/v1/query", params={"query": query}, timeout=10)
    r.raise_for_status()
    assert r.json()["status"] == "success"
    return r.json()["data"]["result"]


def log_query(trace):
    r = httpx.get(
        LOKI + "/loki/api/v1/query_range",
        params={
            "query": '{namespace="sre-demo"} |= "' + trace + '"',
            "since": "10m",
            "limit": 1000,
        },
        timeout=10,
    )
    r.raise_for_status()
    return [
        json.loads(line)
        for stream in r.json()["data"]["result"]
        for _, line in stream["values"]
        if line.startswith("{")
    ]


def trace_check(trace, healthy):
    spans = [s for s in log_query(trace) if s.get("event") == "otel_span"]
    servers = {s["service"] for s in spans if s["kind"] == "SERVER"}
    if healthy:
        if servers != {"gateway", "order", "inventory"}:
            return None
        if len([s for s in spans if s["kind"] == "CLIENT"]) < 2:
            return None
    else:
        if not {"gateway", "order"}.issubset(servers):
            return None
        if not any(
            s["service"] == "order" and s["kind"] == "CLIENT" and s["status"] == "ERROR"
            for s in spans
        ):
            return None
        if "inventory" in servers:
            return None
    ids = {s["span_id"] for s in spans}
    if not all(
        s["parent_span_id"] is None or s["parent_span_id"] in ids for s in spans
    ):
        return None
    return spans


def snapshots(engine):
    with Session(engine) as s:
        return {
            "business_stock": {
                x.sku: x.available
                for x in s.scalars(select(Stock).where(Stock.tenant_id == "demo"))
            },
            "test_rows": {
                m.__tablename__: s.scalar(
                    select(func.count()).select_from(m).where(m.tenant_id != "demo")
                )
                for m in (Order, Reservation, Stock)
            },
            "test_tenants": s.scalar(
                select(func.count()).select_from(Tenant).where(Tenant.id != "demo")
            ),
        }


def new_alert(previous, names, service=None):
    rows = httpx.get(API + "/incidents", timeout=10).json()
    for row in rows:
        if row["id"] in previous or row["title"].split(":")[0] not in names:
            continue
        if service and row["service"] != service:
            continue
        return row


def main():
    runid = "d1-complete-" + uuid.uuid4().hex[:12]
    folder = ROOT / "evaluation/runs" / runid
    folder.mkdir(parents=True)
    os.environ["CLOUDSRE_VERIFY_EVIDENCE_DIR"] = str(folder / "verification")

    def strict_verify():
        response = httpx.post("http://localhost:18084/verify", timeout=35)
        response.raise_for_status()
        result = response.json()
        # Copy returned evidence to this evaluator run while retaining the verifier's own archive.
        path = folder / "verification" / (result["tenant"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        result["verifier_archive"] = result.get("evidence_path")
        result["evidence_path"] = str(path)
        path.write_text(
            json.dumps({"tenant": result["tenant"], "result": result}, indent=2)
        )
        return result

    engine = create_engine(verification_database_url())
    report = {
        "run_id": runid,
        "started_at": now(),
        "checks": {},
        "model_calls": 0,
        "verification_network_path": "independent Docker container -> kind NodePort -> Kubernetes Service -> gateway -> order -> inventory",
        "faults": [],
        "notes": "Fault restoration is evaluator cleanup, not Agent repair.",
    }
    memory = []
    stop_memory = threading.Event()

    def monitor():
        while not stop_memory.is_set():
            proc = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            memory.append({"at": now(), "containers": proc.stdout.splitlines()})
            stop_memory.wait(5)

    memory_thread = threading.Thread(target=monitor, daemon=True)
    memory_thread.start()
    try:
        wait(lambda: httpx.get("http://localhost:18084/health").status_code == 200)
        with business_ports():
            wait(
                lambda: (
                    len([x for x in prom('up{job="demo"}') if x["value"][1] == "1"])
                    == 4
                )
            )
            healthy = strict_verify()
            assert healthy["passed"], healthy
            report["healthy"] = healthy
            report["checks"]["gateway_real_transaction"] = True
            report["healthy_trace"] = wait(
                lambda: trace_check(healthy["trace_id"], True)
            )
            report["checks"]["three_service_real_otel_trace"] = True
            (folder / "progress.json").write_text(json.dumps(report, indent=2))
            # Stop only the synthetic probe temporarily to make exact fixture accounting possible.
            kube("scale", "deployment/probe", "--replicas=0")
            wait(
                lambda: (
                    json.loads(kube("get", "deployment", "probe", "-o", "json"))
                    .get("status", {})
                    .get("replicas", 0)
                    == 0
                )
            )
            before = snapshots(engine)
            times = []
            for _ in range(100):
                started = time.monotonic()
                result = strict_verify()
                assert result["passed"] and result["cleanup"]["cleaned"], result
                times.append(time.monotonic() - started)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                paired = list(pool.map(lambda _: strict_verify(), range(2)))
            assert all(x["passed"] for x in paired), paired
            after = snapshots(engine)
            assert before == after, (before, after)
            report["fixture_lifecycle"] = {
                "runs": 100,
                "parallel_runs": 2,
                "before": before,
                "after": after,
                "median_seconds": statistics.median(times),
            }
            report["checks"]["100_repeats_and_parallel_cleanup_no_stock_drift"] = True
            (folder / "progress.json").write_text(json.dumps(report, indent=2))
            kube("scale", "deployment/probe", "--replicas=1")
            kube("rollout", "status", "deployment/probe", "--timeout=120s")
            wait(
                lambda: any(
                    float(x["value"][1]) == 1
                    for x in prom("sre_business_probe_success")
                )
            )
            # Freeze a healthy release only after an independent business verification.
            records = collect(
                ROOT,
                healthy=True,
                verification_ref=str(
                    folder / "verification" / (healthy["tenant"] + ".json")
                ),
            )
            assert all(
                x["healthy_verified"]
                and x["content_hash"]
                == digest({k: v for k, v in x.items() if k != "content_hash"})
                for x in records
            )
            report["healthy_releases"] = records
            report["checks"]["sanitized_hashed_release_history"] = True

            def fault(
                name,
                inject,
                restore,
                alert_names,
                *,
                high_load=False,
                dns_failure=False,
            ):
                wait(
                    lambda: (
                        not [
                            a
                            for a in httpx.get(PROM + "/api/v1/alerts").json()["data"][
                                "alerts"
                            ]
                            if a["state"] == "firing"
                        ]
                    ),
                    seconds=150,
                )
                oldids = {r["id"] for r in httpx.get(API + "/incidents").json()}
                item = {
                    "name": name,
                    "started_at": now(),
                    "load_requests": [],
                    "restored": False,
                }
                report["faults"].append(item)
                stop = threading.Event()
                load = None
                fixture = VerificationData(evidence_dir=folder / "load-evidence")

                def workload():
                    while not stop.is_set():
                        started = time.monotonic()
                        try:
                            r = httpx.post(
                                GATEWAY_URL + "/orders",
                                json={"sku": fixture.sku, "quantity": 1},
                                headers={
                                    "X-Test-Tenant": fixture.tenant,
                                    "Idempotency-Key": fixture.tenant
                                    + "-"
                                    + uuid.uuid4().hex,
                                },
                                timeout=8,
                            )
                            item["load_requests"].append(
                                {
                                    "at": now(),
                                    "status": r.status_code,
                                    "seconds": time.monotonic() - started,
                                }
                            )
                        except httpx.HTTPError as exc:
                            item["load_requests"].append(
                                {
                                    "at": now(),
                                    "error": type(exc).__name__,
                                    "seconds": time.monotonic() - started,
                                }
                            )
                        stop.wait(1)

                try:
                    item["stage"] = "injecting"
                    inject()
                    item["release_records"] = collect(ROOT)
                    if high_load:
                        fixture.create()
                        load = threading.Thread(target=workload, daemon=True)
                        load.start()
                    item["stage"] = "confirming_target_failure"
                    item["failure_probes"] = []

                    def target_failure():
                        for candidate in item["failure_probes"]:
                            if candidate["passed"]:
                                continue
                            if not dns_failure:
                                return candidate
                            if not candidate.get("trace_id"):
                                continue
                            spans = trace_check(candidate["trace_id"], False)
                            if spans:
                                item["failure_trace"] = spans
                                return candidate
                        candidate = strict_verify()
                        item["failure_probes"].append(candidate)
                        if not dns_failure and not candidate["passed"]:
                            return candidate
                        return None

                    failed = wait(target_failure, seconds=60)
                    item["business_failure"] = failed
                    item["stage"] = "waiting_for_real_alert"
                    alert = wait(lambda: new_alert(oldids, alert_names), seconds=120)
                    item["alert"] = alert
                    item["detected_at"] = now()
                    item["detection_seconds"] = (
                        datetime.fromisoformat(item["detected_at"])
                        - datetime.fromisoformat(item["started_at"])
                    ).total_seconds()
                    assert item["detection_seconds"] <= 120
                    if dns_failure:
                        current_order = json.loads(
                            kube("get", "deployment", "order", "-o", "json")
                        )
                        release = next(
                            e["value"]
                            for e in current_order["spec"]["template"]["spec"][
                                "containers"
                            ][0]["env"]
                            if e["name"] == "RELEASE_ID"
                        )
                        wait(
                            lambda: (
                                (
                                    r := httpx.get(
                                        "http://localhost:18081/health", timeout=2
                                    )
                                ).status_code
                                == 200
                                and r.headers.get("x-release-id") == release
                            ),
                            seconds=30,
                        )
                        item["liveness_did_not_hide_failure"] = True
                finally:
                    stop.set()
                    if load:
                        load.join(timeout=12)
                    restore()
                    item["restored"] = True
                    if high_load:
                        fixture.archive(item)
                        fixture.cleanup()
                    fixture.close()
                    item["healthy_after"] = wait(
                        lambda: r if (r := strict_verify())["passed"] else None,
                        seconds=120,
                    )
                    assert item["healthy_after"]["passed"]
                item["stage"] = "passed"
                report["checks"][name] = True
                (folder / "progress.json").write_text(json.dumps(report, indent=2))

            neutral = "http://svc-" + uuid.uuid4().hex[:8] + ":8000"

            def env(deployment, value, release):
                kube(
                    "set",
                    "env",
                    "deployment/" + deployment,
                    value,
                    "RELEASE_ID=" + release,
                )
                kube("rollout", "status", "deployment/" + deployment, "--timeout=120s")

            fault(
                "config_fault_real_metric_log_alert",
                lambda: env(
                    "order", "INVENTORY_URL=" + neutral, "r-" + uuid.uuid4().hex[:8]
                ),
                lambda: env(
                    "order", "INVENTORY_URL=http://inventory:8000", "v20261023.1"
                ),
                {"OrderErrors"},
                high_load=True,
                dns_failure=True,
            )
            fault(
                "dependency_down_detected",
                lambda: kube("scale", "deployment/inventory", "--replicas=0"),
                lambda: (
                    kube("scale", "deployment/inventory", "--replicas=1"),
                    kube("rollout", "status", "deployment/inventory", "--timeout=120s"),
                ),
                {"DemoTargetDown"},
            )
            fault(
                "low_traffic_business_failure_detected",
                lambda: env(
                    "gateway", "ORDER_URL=" + neutral, "r-" + uuid.uuid4().hex[:8]
                ),
                lambda: env("gateway", "ORDER_URL=http://order:8000", "v20261023.1"),
                {"BusinessProbeFailed"},
            )
            fault(
                "gateway_service_route_unreachable_detected",
                lambda: kube(
                    "patch",
                    "service",
                    "gateway",
                    "--type=json",
                    "-p",
                    '[{"op":"replace","path":"/spec/ports/0/targetPort","value":9999}]',
                ),
                lambda: kube(
                    "patch",
                    "service",
                    "gateway",
                    "--type=json",
                    "-p",
                    '[{"op":"replace","path":"/spec/ports/0/targetPort","value":8000}]',
                ),
                {"DemoTargetDown", "BusinessProbeFailed"},
            )
            report["checks"]["all_faults_restored_by_evaluator"] = all(
                x["restored"] for x in report["faults"]
            )
            report["final_health"] = strict_verify()
            assert report["final_health"]["passed"]
            collect(
                ROOT,
                healthy=True,
                verification_ref=report["final_health"]["evidence_path"],
            )
            report["metrics"] = prom("sum by (service,status)(sre_http_requests_total)")
            report["checks"]["request_counters_and_latency_histogram_present"] = bool(
                prom("sre_http_duration_seconds_count")
            )
            grafana = dotenv_values(ROOT / "infra/.env")
            with httpx.Client(
                base_url="http://localhost:13000",
                auth=("admin", grafana["GRAFANA_PASSWORD"]),
            ) as client:
                ds = client.get("/api/datasources")
                ds.raise_for_status()
                assert {"prometheus", "loki"} <= {x["uid"] for x in ds.json()}
                assert (
                    client.get("/api/dashboards/uid/cloudsre-demo").status_code == 200
                )
                report["checks"]["grafana_datasources_ready"] = True
            rendered = subprocess.check_output(
                [PREFIX[0], "kustomize", str(ROOT / "deploy")], text=True
            )
            report["checks"]["standard_kustomize_render"] = (
                "name: gateway" in rendered and "kind: Secret" not in rendered
            )
            report["source_sha256"] = {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (ROOT / "backend/cloudsre").rglob("*.py")
            }
            report["passed"] = all(report["checks"].values())
    finally:
        # Also recover the probe if fixture stress tests fail before its normal restoration.
        subprocess.run(
            PREFIX + ["scale", "deployment/probe", "--replicas=1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        stop_memory.set()
        memory_thread.join(timeout=20)
        report["memory_samples"] = memory
        report["completed_at"] = now()
        report["passed"] = report.get("passed", False)
        (folder / "result.json").write_text(json.dumps(report, indent=2))
        Path("/mnt/d/uclaude/d1-complete-verification.json").write_text(
            json.dumps(report, indent=2)
        )
        engine.dispose()
    print(
        json.dumps(
            {"run_id": runid, "passed": report["passed"], "checks": report["checks"]}
        )
    )


if __name__ == "__main__":
    main()
