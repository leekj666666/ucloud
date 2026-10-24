"""Independent real-entry traffic and recovery gates; missing evidence fails closed."""

import concurrent.futures
import hashlib
import json
import math
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

VERIFIER = os.environ.get("CLOUDSRE_VERIFIER_URL", "http://localhost:18084")
NAMESPACE = os.environ.get("CLOUDSRE_NAMESPACE", "")
MONITOR_JOB = os.environ.get("CLOUDSRE_MONITOR_JOB", "demo")
PROM = os.environ.get("CLOUDSRE_PROM_URL", "http://localhost:19090")


def now():
    return datetime.now(UTC).isoformat()


def request(path):
    response = httpx.post(VERIFIER + path, timeout=35)
    response.raise_for_status()
    return response.json()


def monitor():
    data = {}
    for key, query in {
        "targets": 'up{job="' + MONITOR_JOB + '"}',
        "requests": 'sum by(service,status)(sre_http_requests_total{route="/orders"'
        + (',namespace="' + NAMESPACE + '"' if NAMESPACE else "")
        + "})",
        "alerts": 'ALERTS{alertstate="firing"'
        + (',namespace="' + NAMESPACE + '"' if NAMESPACE else "")
        + "}",
    }.items():
        r = httpx.get(PROM + "/api/v1/query", params={"query": query}, timeout=8)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError("Monitoring query unavailable")
        data[key] = body["data"]["result"]
    if (
        len(data["targets"]) < 4
        or not data["requests"]
        or not all(math.isfinite(float(x["value"][1])) for x in data["requests"])
    ):
        raise RuntimeError("Monitoring data missing")
    if not all(float(x["value"][1]) == 1 for x in data["targets"]):
        raise RuntimeError("Business monitoring target unavailable")
    return data


def alert_keys(data):
    return {
        json.dumps(x["metric"], sort_keys=True)
        for x in data["alerts"]
        if x["metric"].get("severity") in {"warning", "critical", "error"}
        and (
            x["metric"].get("environment") == "demo"
            or x["metric"].get("service")
            in {"gateway", "order", "inventory", "business-probe"}
        )
    }


def resources():
    snapshot_path = os.environ.get("CLOUDSRE_RESOURCE_SNAPSHOT")
    if snapshot_path:
        data = json.loads(Path(snapshot_path).read_text())
        if time.time() - data["sampled_unix"] > 20:
            raise RuntimeError("Resource observer snapshot stale")
        return data
    p = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=12,
        check=True,
    )
    return {
        "containers": [json.loads(x) for x in p.stdout.splitlines()],
        "meminfo": Path("/proc/meminfo").read_text(),
    }


def p95(values):
    if not values or not all(math.isfinite(x) and x >= 0 for x in values):
        raise ValueError("Invalid latency samples")
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def evaluate(
    records, duration, baseline_p95=None, expected_samples=120, expected_full=12
):
    samples = [r for r in records if r["kind"] == "load"]
    full = [r for r in records if r["kind"] == "business"]
    monitors = [r for r in records if r["kind"] == "monitor"]
    metrics = [r["result"] for r in monitors if "result" in r]
    initial = alert_keys(metrics[0]) if metrics else set()
    latency = [
        r["result"]["latency_seconds"]
        for r in samples
        if "result" in r and "latency_seconds" in r["result"]
    ]
    measured = p95(latency) if len(latency) == len(samples) and latency else None
    threshold = max(baseline_p95 * 2, 1.0) if baseline_p95 is not None else None
    checks = {
        "duration_at_least_120": duration >= 120,
        "all_scheduled_requests_recorded": len(samples) >= expected_samples,
        "business_checks_at_least_12": len(full) >= expected_full,
        "all_requests_succeeded": bool(samples)
        and all(
            r.get("result", {}).get("passed") is True
            and r["result"].get("http_status") == 200
            for r in samples
        ),
        "all_invariants_succeeded": bool(full)
        and all(
            r.get("result", {}).get("passed") is True
            and r["result"].get("cleanup", {}).get("cleaned") is True
            for r in full
        ),
        "monitoring_available": len(monitors) >= 24 and len(metrics) == len(monitors),
        "no_new_related_severe_alert": bool(metrics)
        and all(not (alert_keys(m) - initial) for m in metrics),
        "resource_samples_present": any(
            r["kind"] == "resources" and r.get("result", {}).get("containers")
            for r in records
        ),
        "p95_within_frozen_limit": measured is not None
        and (threshold is None or measured <= threshold),
        "traffic_schedule_preserved": bool(samples)
        and all(r.get("scheduling_delay_seconds", 100) < 1 for r in samples),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "duration_seconds": duration,
        "load_requests": len(samples),
        "business_checks": len(full),
        "p95_seconds": measured,
        "baseline_p95_seconds": baseline_p95,
        "p95_limit_seconds": threshold,
        "errors": sum(not r.get("result", {}).get("passed", False) for r in samples),
        "http_5xx": sum(
            r.get("result", {}).get("http_status", 0) >= 500 for r in samples
        ),
        "unknown_or_transport_errors": sum("error_type" in r for r in samples),
    }


def run_window(folder, baseline=None, seconds=120):
    if seconds != 120:
        raise ValueError("D2 formal acceptance window is fixed at 120 seconds")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    lock = threading.Lock()
    started = time.monotonic()
    started_at = now()
    output = (folder / "samples.jsonl").open("w")

    def collect(kind, index, scheduled, fn):
        entry = {
            "kind": kind,
            "index": index,
            "at": now(),
            "scheduling_delay_seconds": max(0, time.monotonic() - scheduled),
        }
        begin = time.monotonic()
        try:
            entry["result"] = fn()
        except (
            ValueError,
            RuntimeError,
            OSError,
            KeyError,
            TypeError,
            httpx.HTTPError,
            subprocess.SubprocessError,
        ) as exc:
            entry["error_type"] = type(exc).__name__
        entry["elapsed_seconds"] = time.monotonic() - begin
        with lock:
            records.append(entry)
            output.write(json.dumps(entry) + "\n")
            output.flush()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for tick in range(seconds):
                scheduled = started + tick
                time.sleep(max(0, scheduled - time.monotonic()))
                pool.submit(
                    collect, "load", tick, scheduled, lambda: request("/sample")
                )
                if tick % 10 == 0:
                    pool.submit(
                        collect,
                        "business",
                        tick // 10,
                        scheduled,
                        lambda: request("/verify"),
                    )
                    pool.submit(collect, "resources", tick // 10, scheduled, resources)
                if tick % 5 == 0:
                    pool.submit(collect, "monitor", tick // 5, scheduled, monitor)
            time.sleep(max(0, started + seconds - time.monotonic()))
    finally:
        output.close()
    duration = time.monotonic() - started
    records.sort(key=lambda r: (r["at"], r["kind"]))
    # Monitoring order is determined by schedule, not network completion.
    monitoring = sorted(
        [r for r in records if r["kind"] == "monitor"], key=lambda r: r["index"]
    )
    records = [r for r in records if r["kind"] != "monitor"] + monitoring
    result = evaluate(records, duration, baseline["p95_seconds"] if baseline else None)
    if (
        baseline is None
        and monitoring
        and "result" in monitoring[0]
        and alert_keys(monitoring[0]["result"])
    ):
        result["checks"]["healthy_baseline_no_related_alerts"] = False
        result["passed"] = False
    peaks = {}
    for row in records:
        if row["kind"] != "resources" or "result" not in row:
            continue
        for container in row["result"]["containers"]:
            value = container.get("MemUsage", "").split("/")[0].strip()
            import re

            match = re.fullmatch(r"([0-9.]+)([A-Za-z]+)", value)
            if match:
                factors = {
                    "B": 1,
                    "kB": 1000,
                    "KB": 1000,
                    "KiB": 1024,
                    "MB": 1000000,
                    "MiB": 1048576,
                    "GB": 1000000000,
                    "GiB": 1073741824,
                }
                size = float(match[1]) * factors.get(match[2], 1)
                key = container.get("Name", container.get("Container", "unknown"))
                peaks[key] = max(peaks.get(key, 0), size)
    result["sampled_peak_container_memory_bytes"] = peaks
    result["resource_sampling_seconds"] = 10
    result.update(
        {
            "started_at": started_at,
            "ended_at": now(),
            "requests_per_second": 1,
            "samples_path": str(folder / "samples.jsonl"),
            "samples_sha256": hashlib.sha256(
                (folder / "samples.jsonl").read_bytes()
            ).hexdigest(),
            "network_path": "Docker -> NodePort -> Kubernetes Service -> gateway",
        }
    )
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    return result
