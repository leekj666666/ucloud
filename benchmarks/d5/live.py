"""Evaluator-only live fixtures, evidence capture and exact-field restoration."""

import contextlib
import fcntl
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
from cloudsre.durable import digest, now
from cloudsre.evidence_v3 import collect
from cloudsre.quality import RecoveryReadiness, alert_keys, monitor, request

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "evaluation/d5-runtime"
PRIVATE = ROOT / "evaluation/d5-private"
KUBE = [
    "/home/lee/sre-tools/bin/kubectl",
    "--context",
    "kind-sre-dev",
    "-n",
    "sre-demo",
]


def kube(*args):
    return subprocess.check_output(KUBE + list(args), text=True)


def read(kind, name):
    return json.loads(kube("get", kind, name, "-o", "json"))


def field(raw, pointer):
    value = raw
    for part in pointer.strip("/").split("/"):
        value = (
            value[int(part)]
            if isinstance(value, list)
            else value[part.replace("~1", "/").replace("~0", "~")]
        )
    return value


@contextlib.contextmanager
def exclusive():
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "business.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def configure():
    os.environ.update(
        CLOUDSRE_READ_KUBECONFIG=str(ROOT / "infra/.generated/d4/read-host.yaml"),
        CLOUDSRE_EXECUTE_KUBECONFIG=str(ROOT / "infra/.generated/d4/execute-host.yaml"),
        CLOUDSRE_TRUSTED_BASELINE=str(ROOT / "infra/.generated/d4/baseline.json"),
        CLOUDSRE_LOKI_URL="http://localhost:13100",
        CLOUDSRE_PROM_URL="http://localhost:19090",
        CLOUDSRE_NAMESPACE="sre-demo",
        CLOUDSRE_RESOURCE_SNAPSHOT=str(
            ROOT / "evaluation/d4-runtime/observer/resources.json"
        ),
    )
    # quality's constants are frozen at import; make this explicit in evaluator setup.
    from cloudsre import quality

    quality.NAMESPACE = "sre-demo"
    quality.MONITOR_JOB = "demo"


def wait_healthy(folder):
    gate = RecoveryReadiness()
    deadline = time.monotonic() + 180
    rows = []
    while time.monotonic() < deadline:
        row = {"at": now()}
        try:
            result = request("/verify")
            metrics = monitor()
            row.update(business_passed=result.get("passed"), alerts=metrics["alerts"])
            ready = gate.observe(
                result.get("passed") is True, metrics, time.monotonic()
            )
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            row["error_type"] = type(exc).__name__
            ready = gate.observe(False, None, time.monotonic())
        rows.append(row)
        (folder / "preflight.json").write_text(json.dumps(rows, indent=2))
        if ready:
            return
        time.sleep(2)
    raise RuntimeError(
        "Independent healthy/monitoring precondition failed; no further injection"
    )


def confirm_release_order(case, fixture, records):
    if not case.distractor:
        return
    releases = records["releases"]["data"].get("release_revisions", [])
    if not releases:
        raise ValueError("Newer unrelated release is missing from actual evidence")
    latest = max(releases, key=lambda row: (row["created_at"], row["uid"]))
    if any(
        row["uid"] != latest["uid"] and row["created_at"] >= latest["created_at"]
        for row in releases
    ):
        raise ValueError(
            "Timestamp tie cannot prove a strictly newer unrelated release"
        )
    value = fixture.record["changes"][-1]["injected"]
    owners = {row["name"] for row in latest["owners"]}
    release_ids = {
        container.get("env", {}).get("RELEASE_ID") for container in latest["containers"]
    }
    if case.distractor not in owners or value not in release_ids:
        raise ValueError(
            "Actual newest rollout is not the controlled unrelated release"
        )
    fixture.record["newest_unrelated_release_uid"] = latest["uid"]
    fixture.persist()


class Fixture:
    def __init__(self, case, folder):
        self.case = case
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "fixture.json"
        self.record = {
            "case": case.public_description(),
            "fixture_protocol_version": 2,
            "created_at": now(),
            "changes": [],
            "active": False,
            "agent_has_no_fixture_mount": True,
        }
        self.stop = threading.Event()
        self.traffic = None

    def persist(self):
        self.path.write_text(json.dumps(self.record, indent=2))

    def change(self, kind, name, pointer, value):
        before = read(kind, name)
        item = {
            "kind": kind,
            "name": name,
            "uid": before["metadata"]["uid"],
            "path": pointer,
            "original": field(before, pointer),
            "injected": value,
        }
        self.record["changes"].append(item)
        self.record["active"] = True
        self.persist()
        for attempt in range(5):
            current = read(kind, name)
            if (
                current["metadata"]["uid"] != item["uid"]
                or field(current, pointer) != item["original"]
            ):
                raise ValueError("Resource changed before exact fixture injection")
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": item["uid"]},
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": current["metadata"]["resourceVersion"],
                },
                {"op": "test", "path": pointer, "value": item["original"]},
                {"op": "replace", "path": pointer, "value": value},
            ]
            try:
                kube("patch", kind, name, "--type=json", "-p", json.dumps(patch))
                return
            except subprocess.CalledProcessError:
                if attempt == 4:
                    raise
                time.sleep(0.2)

    def env_change(self, name, key, value):
        raw = read("deployment", name)
        containers = raw["spec"]["template"]["spec"]["containers"]
        ci = next(i for i, c in enumerate(containers) if c["name"] == name)
        ei = next(i for i, e in enumerate(containers[ci]["env"]) if e["name"] == key)
        self.change(
            "deployment",
            name,
            f"/spec/template/spec/containers/{ci}/env/{ei}/value",
            value,
        )

    def inject(self):
        c = self.case
        self.record["root_change_index"] = len(self.record["changes"])
        self.record["fault_at"] = now()
        self.persist()
        if c.family == "config":
            upstream = "inventory" if c.target == "order" else "order"
            value = {
                "missing_dns": "http://"
                + upstream
                + "-"
                + uuid.uuid4().hex[:8]
                + ":8000",
                "wrong_url_port": "http://" + upstream + ":8019",
                "wrong_url_path": "http://" + upstream + ":8000/absent-prefix",
                "wrong_url_scheme": "https://" + upstream + ":8000",
            }[c.mechanism]
            self.env_change(
                c.target, "INVENTORY_URL" if c.target == "order" else "ORDER_URL", value
            )
            kube("rollout", "status", "deployment/" + c.target, "--timeout=120s")
        elif c.family == "route":
            self.change(
                "service",
                c.target,
                "/spec/ports/0/targetPort",
                "unbound-http" if c.mechanism == "unbound_named_port" else 8011,
            )
        elif c.family == "dependency":
            self.change("deployment", c.target, "/spec/replicas", 0)
        elif c.family == "resources":
            self.change(
                "deployment",
                c.target,
                "/spec/template/spec/containers/0/resources",
                {
                    "requests": {"cpu": "20m", "memory": "8Mi"},
                    "limits": {"cpu": "200m", "memory": "8Mi"},
                },
            )
        elif c.family == "unsupported":
            self.change(
                "service",
                c.target,
                "/spec/selector",
                {
                    "app": "order"
                    if c.mechanism == "selector_wrong_backend"
                    else "absent-backend"
                },
            )
        else:
            raise ValueError("Unsupported controlled case")
        if c.distractor:
            if c.family in {"config", "resources"}:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    current = read("deployment", c.target)
                    if (
                        current.get("status", {}).get("observedGeneration", 0)
                        >= current["metadata"]["generation"]
                    ):
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(
                        "Root rollout was not observed before unrelated release"
                    )
            # ReplicaSet creation timestamps have one-second precision. Keep the
            # two controlled changes distinguishable in the actual public history.
            time.sleep(1.1)
            self.record["distractor_at"] = now()
            self.record["distractor_after_fault"] = True
            self.env_change(c.distractor, "RELEASE_ID", "r-" + uuid.uuid4().hex[:12])
            kube("rollout", "status", "deployment/" + c.distractor, "--timeout=120s")
        self.traffic = threading.Thread(target=self.load, daemon=True)
        self.traffic.start()
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            current = self.samples()
            business_failed = any(
                row.get("result", {}).get("passed") is False for row in current
            )
            try:
                response = httpx.get(
                    os.environ["CLOUDSRE_PROM_URL"] + "/api/v1/query",
                    params={
                        "query": 'ALERTS{alertstate="firing",namespace="sre-demo"}'
                    },
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") != "success":
                    raise RuntimeError("Fault alert query failed")
                alerts = alert_keys({"alerts": payload["data"]["result"]})
            except (RuntimeError, httpx.HTTPError):
                alerts = set()
            oom = False
            if c.family == "resources":
                pods = json.loads(
                    kube("get", "pods", "-l", "app=" + c.target, "-o", "json")
                )["items"]
                oom = any(
                    s.get(k, {}).get("terminated", {}).get("reason") == "OOMKilled"
                    for p in pods
                    for s in p.get("status", {}).get("containerStatuses", [])
                    for k in ("state", "lastState")
                )
            if (
                len(current) >= 3
                and alerts
                and (oom if c.family == "resources" else business_failed)
            ):
                self.record.update(
                    fault_confirmed=True,
                    business_failure_observed=business_failed,
                    oom_observed=oom,
                    alerts=list(alerts),
                )
                self.persist()
                return
            time.sleep(2)
        raise RuntimeError("Real fault and alert preconditions were not observed")

    def load(self):
        with (self.folder / "fault-traffic.jsonl").open("a") as stream:
            while not self.stop.is_set():
                row = {"at": now()}
                try:
                    row["result"] = request("/sample")
                except (httpx.HTTPError, ValueError) as exc:
                    row["error_type"] = type(exc).__name__
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                self.stop.wait(1)

    def samples(self):
        path = self.folder / "fault-traffic.jsonl"
        rows = []
        for line in path.read_text().splitlines() if path.exists() else []:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    def drain(self):
        self.stop.set()
        if self.traffic:
            self.traffic.join(timeout=45)
            if self.traffic.is_alive():
                raise RuntimeError("Fault traffic did not drain")

    def cleanup(self):
        self.drain()
        used = []
        for item in reversed(self.record["changes"]):
            for attempt in range(5):
                current = read(item["kind"], item["name"])
                observed = field(current, item["path"])
                if current["metadata"]["uid"] != item["uid"]:
                    raise ValueError("Recreated resource prevents evaluator cleanup")
                if observed == item["original"]:
                    break
                if observed != item["injected"]:
                    raise ValueError("Foreign field value prevents evaluator cleanup")
                patch = [
                    {"op": "test", "path": "/metadata/uid", "value": item["uid"]},
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": current["metadata"]["resourceVersion"],
                    },
                    {"op": "test", "path": item["path"], "value": observed},
                    {"op": "replace", "path": item["path"], "value": item["original"]},
                ]
                try:
                    kube(
                        "patch",
                        item["kind"],
                        item["name"],
                        "--type=json",
                        "-p",
                        json.dumps(patch),
                    )
                    used.append(item["name"] + item["path"])
                    break
                except subprocess.CalledProcessError:
                    if attempt == 4:
                        raise
                    time.sleep(0.2)
        for name in {
            item["name"]
            for item in self.record["changes"]
            if item["kind"] == "deployment"
        }:
            kube("rollout", "status", "deployment/" + name, "--timeout=120s")
        self.record.update(
            active=False, cleanup_at=now(), evaluator_cleanup_fields=used
        )
        self.persist()
        return used


def capture(case, folder):
    fixture = Fixture(case, folder)
    wait_healthy(fixture.folder)
    snapshot_id = uuid.uuid4().hex
    snapshot_dir = WORK / "snapshots" / snapshot_id
    snapshot_dir.mkdir(parents=True)
    result = {
        "case_id": case.id,
        "split": case.split,
        "snapshot_id": snapshot_id,
        "started_at": now(),
        "passed": False,
    }
    try:
        fixture.inject()
        incident = {
            "id": uuid.uuid4().hex,
            "title": "Observed business or workload alert; investigate current evidence",
            "service": "order",
            "namespace": "sre-demo",
            "evidence_since": fixture.record["fault_at"],
        }
        (snapshot_dir / "incident.json").write_text(json.dumps(incident))
        records = {}
        from cloudsre.kube_gateway import KubeGateway

        gateway = KubeGateway("read")
        try:
            raw_current = {
                f"sre-demo/{kind}/{name}": gateway.read(f"sre-demo/{kind}/{name}")
                for kind in ("Deployment", "Service")
                for name in ("gateway", "order", "inventory")
            }
        finally:
            gateway.close()
        (fixture.folder / "current-raw.json").write_text(json.dumps(raw_current))
        for kind in (
            "resources",
            "observations",
            "baseline",
            "releases",
            "logs",
            "metrics",
        ):
            if case.tool_failure and kind == "logs":
                original = os.environ["CLOUDSRE_LOKI_URL"]
                os.environ["CLOUDSRE_LOKI_URL"] = "http://localhost:1"
            try:
                data = collect(kind, snapshot_dir)
            except (httpx.HTTPError, RuntimeError) as exc:
                data = {
                    "collection_error": {
                        "error_type": type(exc).__name__,
                        "source": "real read-only transport failure",
                    }
                }
            finally:
                if case.tool_failure and kind == "logs":
                    os.environ["CLOUDSRE_LOKI_URL"] = original
            record = {
                "id": "e-" + uuid.uuid4().hex,
                "kind": kind,
                "collected_at": now(),
                "source": "live-read-only-tool",
                "data": data,
                "content_hash": digest(data),
            }
            (snapshot_dir / (kind + ".json")).write_text(
                json.dumps(record, ensure_ascii=False)
            )
            records[kind] = record
        confirm_release_order(case, fixture, records)
        result.update(
            scoring_ready=True,
            capture_folder=str(fixture.folder),
            passed=True,
            incident_sha256=digest(incident),
            records_sha256={kind: digest(record) for kind, record in records.items()},
            fault_confirmed=True,
            fixture_protocol_version=fixture.record["fixture_protocol_version"],
            distractor_after_fault=fixture.record.get("distractor_after_fault", False),
            semantics="Frozen real observations for diagnostic replay; not an autonomous recovery result",
        )
        return result
    except BaseException as exc:
        result.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:500])
        raise
    finally:
        try:
            fixture.cleanup()
        except BaseException as exc:
            result.update(
                passed=False,
                cleanup_error_type=type(exc).__name__,
                cleanup_error=str(exc)[:500],
            )
            raise
        finally:
            result["ended_at"] = now()
            (fixture.folder / "result.json").write_text(json.dumps(result, indent=2))
