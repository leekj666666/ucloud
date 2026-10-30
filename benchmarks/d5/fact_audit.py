"""Evaluator-only, model-free audit of frozen raw-to-compact evidence facts."""

import argparse
import hashlib
import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

from cloudsre.durable import digest, now
from cloudsre.experiment_views import KINDS, load_records, view

from .catalog import DEV, VALIDATION
from .live import PRIVATE, ROOT, WORK

MISSING = object()
CONTAINER_FIELDS = ("name", "image", "env", "resources", "ports")
STATE_FIELDS = ("name", "ready", "restartCount", "state", "lastState")


def pack(value):
    return {
        "present": value is not MISSING,
        "value": None if value is MISSING else value,
    }


class Audit:
    def __init__(self):
        self.checks = []

    def check(self, name, expected, observed, *, critical=True):
        before, after = pack(expected), pack(observed)
        self.checks.append(
            {
                "name": name,
                "status": "preserved" if before == after else "lost",
                "critical": critical,
                "source_sha256": digest(before),
                "compact_sha256": digest(after),
            }
        )

    def note(self, name, status, reason, **details):
        self.checks.append(
            {
                "name": name,
                "status": status,
                "critical": False,
                "reason": reason,
                **details,
            }
        )


def resource_key(item):
    return item["identity"]["resource"]


def resources(audit, kind, raw, compact):
    originals = {resource_key(item): item for item in raw["resources"]}
    outputs = {resource_key(item): item for item in compact["resources"]}
    audit.check(kind + ".resource_set", sorted(originals), sorted(outputs))
    for key, item in originals.items():
        prefix = kind + "." + key
        output = outputs.get(key, {})
        audit.check(
            prefix + ".identity", item["identity"], output.get("identity", MISSING)
        )
        for name in ("created_at", "annotations"):
            audit.check(
                prefix + "." + name, item.get(name, MISSING), output.get(name, MISSING)
            )
        spec, short = item["spec"], output.get("spec", {})
        if "template" in spec:
            audit.check(
                prefix + ".replicas",
                spec.get("replicas", MISSING),
                short.get("replicas", MISSING),
            )
            old = {
                container["name"]: container
                for container in spec["template"]["spec"]["containers"]
            }
            new = {
                container["name"]: container
                for container in short.get("containers", [])
            }
            audit.check(prefix + ".container_set", sorted(old), sorted(new))
            for name, container in old.items():
                for field in CONTAINER_FIELDS:
                    audit.check(
                        prefix + ".containers." + name + "." + field,
                        container.get(field, MISSING),
                        new.get(name, {}).get(field, MISSING),
                    )
            omitted = sorted(set(spec) - {"replicas", "template"})
            audit.note(
                prefix + ".other_deployment_fields",
                "intentional_omission",
                "Deployment selector, rollout strategy, Pod template metadata and non-allowlisted Pod/container settings are not a lossless full-spec view.",
                top_level_fields=omitted,
            )
        else:
            for field in ("ports", "selector", "type"):
                audit.check(
                    prefix + "." + field,
                    spec.get(field, MISSING),
                    short.get(field, MISSING),
                )
            audit.note(
                prefix + ".other_service_fields",
                "intentional_omission",
                "Only ports, selector and service type are in the compact Service spec.",
                fields=sorted(set(spec) - {"ports", "selector", "type"}),
            )
        if item.get("status"):
            audit.note(
                prefix + ".resource_status",
                "intentional_omission",
                "Deployment/Service status is omitted from resource/baseline views; live Pod readiness is audited separately.",
            )
    for name in set(raw) - {"resources"}:
        audit.check(kind + "." + name, raw[name], compact.get(name, MISSING))


def event_projection(event):
    return {
        "resource": event.get("involvedObject", {}).get("name"),
        "reason": event.get("reason"),
        "type": event.get("type"),
        "message": event.get("message"),
        "last_at": event.get("lastTimestamp"),
        "count": event.get("count"),
    }


def observations(audit, raw, compact, target):
    old_pods = {pod["name"]: pod for pod in raw["pods"]}
    pods = {pod["name"]: pod for pod in compact.get("pods", [])}
    selected = {
        name: pod
        for name, pod in old_pods.items()
        if any(
            name.startswith(owner + "-") for owner in ("gateway", "order", "inventory")
        )
    }
    audit.check("runtime.business_pod_set", sorted(selected), sorted(pods))
    for name, pod in selected.items():
        output = pods.get(name, {})
        prefix = "runtime." + name
        audit.check(
            prefix + ".phase", pod["status"].get("phase"), output.get("phase", MISSING)
        )
        conditions = [
            {
                "type": condition["type"],
                "status": condition["status"],
                "reason": condition.get("reason"),
            }
            for condition in pod["status"].get("conditions", [])
        ]
        audit.check(
            prefix + ".readiness_conditions",
            conditions,
            output.get("conditions", MISSING),
        )
        audit.check(
            prefix + ".containers_resources",
            pod["containers"],
            output.get("containers", MISSING),
        )
        states = [
            {key: state[key] for key in STATE_FIELDS if key in state}
            for state in pod["status"].get("containerStatuses", [])
        ]
        audit.check(
            prefix + ".ready_restarts_current_and_last_state",
            states,
            output.get("container_states", MISSING),
        )
    excluded = sorted(set(old_pods) - set(selected))
    audit.note(
        "runtime.non_business_pods",
        "intentional_omission",
        "Non-business Pod names are filtered; this does not establish complete namespace diagnostics.",
        count=len(excluded),
        names=excluded,
    )
    audit.note(
        "runtime.other_pod_metadata",
        "intentional_omission",
        "Pod labels/owners, condition timestamps/messages, host/Pod IP, container imageID and other status metadata are omitted. Ownership evidence is not globally lossless.",
    )
    endpoints = [
        {
            "service": item["metadata"]
            .get("labels", {})
            .get("kubernetes.io/service-name"),
            "ports": item.get("ports"),
            "endpoints": item.get("endpoints"),
        }
        for item in raw["endpoints"]
    ]
    audit.check(
        "runtime.endpoint_addresses_ports_readiness_and_service",
        endpoints,
        compact.get("endpoints", MISSING),
    )
    audit.note(
        "runtime.endpoint_slice_metadata",
        "intentional_omission",
        "EndpointSlice metadata other than service name is omitted; address/port/condition payloads are checked exactly.",
    )
    events = sorted(
        raw["events"],
        key=lambda event: (
            event.get("lastTimestamp") or event["metadata"]["creationTimestamp"]
        ),
    )
    audit.check(
        "events.latest_12_payload",
        [event_projection(event) for event in events[-12:]],
        compact.get("events", MISSING),
    )
    audit.check(
        "events.fetched_count", len(events), compact.get("event_count_fetched", MISSING)
    )
    audit.check("events.view_limit", 12, compact.get("event_view_limit", MISSING))
    audit.check(
        "events.query_limit_and_upstream_truncation",
        raw.get("event_query", MISSING),
        compact.get("event_query", MISSING),
    )
    omitted_events = events[:-12] if len(events) > 12 else []
    possibly_relevant = [
        event
        for event in omitted_events
        if event.get("involvedObject", {}).get("name", "").startswith(target)
        and (
            event.get("type") == "Warning"
            or event.get("reason")
            in {"OOMKilled", "Killing", "BackOff", "Unhealthy", "Failed"}
        )
    ]
    event_loss = {
        "fetched": len(events),
        "retained": min(12, len(events)),
        "omitted": len(omitted_events),
        "query_limit": raw.get("event_query", {}).get("limit"),
        "upstream_truncated": raw.get("event_query", {}).get("truncated"),
        "view_omission_indicated_by_count_and_limit": compact.get("event_count_fetched")
        == len(events)
        and compact.get("event_view_limit") == 12,
        "explicit_view_truncated_boolean_present": "view_truncated" in compact,
        "possibly_relevant_omitted_count": len(possibly_relevant),
        "possibly_relevant_omitted_reasons": dict(
            Counter(event.get("reason") for event in possibly_relevant)
        ),
    }
    audit.note(
        "events.older_payload_loss",
        "intentional_omission",
        "All events older than the newest 12 fetched are discarded from the model view. Count/limit and upstream truncation are retained, but no explicit view_truncated boolean exists. Warning history may be lost; full evidence remains outside the model view.",
        **event_loss,
    )
    audit.note(
        "events.metadata_loss",
        "intentional_omission",
        "Even retained events omit event UID, involvedObject UID/version/kind, source and firstTimestamp; this is not a lossless full event record.",
    )
    for name in set(raw) - {"pods", "events", "endpoints", "event_query"}:
        audit.check("runtime.other." + name, raw[name], compact.get(name, MISSING))
    raw_oom = [
        state
        for name, pod in selected.items()
        if name.startswith(target + "-")
        for state in pod["status"].get("containerStatuses", [])
        if any(
            state.get(slot, {}).get("terminated", {}).get("reason") == "OOMKilled"
            for slot in ("state", "lastState")
        )
    ]
    short_oom = [
        state
        for name, pod in pods.items()
        if name.startswith(target + "-")
        for state in pod.get("container_states", [])
        if any(
            state.get(slot, {}).get("terminated", {}).get("reason") == "OOMKilled"
            for slot in ("state", "lastState")
        )
    ]
    audit.check("runtime.target_oom_state_count", len(raw_oom), len(short_oom))
    return event_loss, len(raw_oom)


def decode_timeline(timeline):
    columns = timeline["columns"]
    if (
        set(columns)
        != {"replicaset_uid", "created_at", "revision", "configuration_index"}
        or len(columns) != 4
    ):
        raise ValueError("Unexpected timeline columns")
    result = []
    for group in timeline["groups"]:
        for row in group["revisions"]:
            if len(row) != len(columns):
                raise ValueError("Timeline row width mismatch")
            fields = dict(zip(columns, row, strict=True))
            index = fields["configuration_index"]
            if type(index) is not int or not 0 <= index < len(group["configurations"]):
                raise ValueError("Invalid timeline configuration reference")
            config = group["configurations"][index]
            result.append(
                {
                    "owner_name": group["deployment"],
                    "owner_uid": group["deployment_uid"],
                    "uid": fields["replicaset_uid"],
                    "created_at": fields["created_at"],
                    "revision": fields["revision"],
                    "containers": config["containers"],
                    "image_content_id": config.get("image_content_id"),
                }
            )
    return result


def normalized_revisions(revisions):
    return [
        {
            "owner_name": owner["name"],
            "owner_uid": owner["uid"],
            "uid": item["uid"],
            "created_at": item["created_at"],
            "revision": item.get("revision"),
            "containers": item["containers"],
            "image_content_id": item.get("image_content_id"),
        }
        for item in revisions
        for owner in item["owners"]
    ]


def revision_differences(revisions):
    groups = {}
    for item in revisions:
        groups.setdefault(item["owner_uid"], []).append(item)
    differences = {}
    for owner, items in groups.items():
        items.sort(key=lambda item: (item["created_at"], item["uid"]))
        for before, after in pairwise(items):

            def fields(item):
                return {
                    container["name"] + "." + key: value
                    for container in item["containers"]
                    for key, value in {
                        "image": container["image"],
                        **container.get("env", {}),
                    }.items()
                } | {"image_content_id": item.get("image_content_id")}

            left, right = fields(before), fields(after)
            differences[owner + "/" + before["uid"] + "/" + after["uid"]] = {
                key: {"before": left.get(key), "after": right.get(key)}
                for key in left.keys() | right.keys()
                if left.get(key) != right.get(key)
            }
    return differences


def releases(audit, raw, compact, fixture, case):
    if "release_revisions" not in raw:
        audit.note(
            "releases.source", "unverifiable", "Raw release revisions are unavailable"
        )
        return
    timeline = compact.get("release_timeline", {})
    try:
        decoded = decode_timeline(timeline)
    except (ValueError, KeyError, TypeError) as exc:
        audit.check("releases.timeline_decodable", True, False)
        audit.note("releases.decode_reason", "unverifiable", str(exc))
        return
    expected = normalized_revisions(raw["release_revisions"])
    audit.check(
        "releases.all_revision_uid_time_owner_configuration_facts",
        sorted(digest(item) for item in expected),
        sorted(digest(item) for item in decoded),
    )
    audit.check(
        "releases.fetched_revision_count",
        len(raw["release_revisions"]),
        timeline.get("revision_count", MISSING),
    )
    audit.check(
        "releases.timestamp_semantics",
        "ReplicaSet creation time; not CI publication time",
        timeline.get("timestamp_semantics", MISSING),
    )
    for owner in sorted({item["owner_uid"] for item in expected}):
        old = [item for item in expected if item["owner_uid"] == owner]
        new = [item for item in decoded if item["owner_uid"] == owner]
        audit.check(
            "releases.latest_per_workload." + owner,
            max(old, key=lambda item: (item["created_at"], item["uid"])),
            max(new, key=lambda item: (item["created_at"], item["uid"]))
            if new
            else MISSING,
        )
    audit.check(
        "releases.all_adjacent_configuration_differences",
        revision_differences(expected),
        revision_differences(decoded),
    )
    audit.note(
        "releases.other_revision_metadata",
        "intentional_omission",
        "ReplicaSet name, namespace, generation, resourceVersion and per-record config checksum are omitted; revision UID/time/owner/configurations and computable config differences are checked.",
    )
    if case.distractor:
        latest = (
            max(decoded, key=lambda item: (item["created_at"], item["uid"]))
            if decoded
            else None
        )
        raw_latest = (
            max(expected, key=lambda item: (item["created_at"], item["uid"]))
            if expected
            else None
        )
        audit.check("releases.latest_global_owner_configuration", raw_latest, latest)
        expected_uid = fixture.get("newest_unrelated_release_uid")
        if expected_uid is None:
            audit.note(
                "releases.fixture_newest_unrelated_uid",
                "unverifiable",
                "Legacy fixture has no separately recorded unrelated-latest UID; raw/compact equality is checked without inventing it.",
            )
        else:
            audit.check(
                "releases.fixture_newest_unrelated_uid",
                expected_uid,
                latest.get("uid") if latest else MISSING,
            )
            audit.check(
                "releases.fixture_newest_unrelated_owner",
                case.distractor,
                latest.get("owner_name") if latest else MISSING,
            )
            changes = [
                change
                for change in fixture["changes"]
                if change["name"] == case.distractor
            ]
            injected_ids = {
                change["injected"]
                for change in changes
                if isinstance(change["injected"], str)
            }
            actual_ids = {
                container.get("env", {}).get("RELEASE_ID")
                for container in (latest or {}).get("containers", [])
            }
            audit.check(
                "releases.fixture_unrelated_release_id_locatable",
                bool(injected_ids),
                bool(injected_ids & actual_ids),
            )


def extract_repair_field(item, pointer, private_current, *, compact):
    parts = pointer.strip("/").split("/")
    if parts[:4] == ["spec", "template", "spec", "containers"] and len(parts) >= 6:
        source_container = private_current["spec"]["template"]["spec"]["containers"][
            int(parts[4])
        ]
        containers = (
            item["spec"]["containers"]
            if compact
            else item["spec"]["template"]["spec"]["containers"]
        )
        container = next(
            row for row in containers if row["name"] == source_container["name"]
        )
        if parts[5] == "resources" and len(parts) == 6:
            return container.get("resources", MISSING)
        if parts[5] == "env" and parts[7:] == ["value"]:
            env_name = source_container["env"][int(parts[6])]["name"]
            return next(
                row["value"] for row in container["env"] if row["name"] == env_name
            )
        raise ValueError("Unsupported evaluator repair pointer")
    value = item
    for part in parts:
        value = (
            value[int(part)]
            if isinstance(value, list)
            else value[part.replace("~1", "/").replace("~0", "~")]
        )
    return value


def fixture_root(audit, records, compacts, fixture, private_current):
    index = fixture.get("root_change_index")
    if index is None and len(fixture["changes"]) == 1:
        index = 0
        audit.note(
            "fixture.root_index_provenance",
            "unverifiable",
            "早期单变更记录只有一次可唯一定位的注入，但采集时未记录 root_change_index；不伪称该索引当时已冻结。",
        )
    if type(index) is not int or not 0 <= index < len(fixture["changes"]):
        audit.note(
            "fixture.root_field",
            "unverifiable",
            "No unambiguous root_change_index; a later unrelated change is never guessed as the root.",
        )
        return
    change = fixture["changes"][index]
    key = (
        "sre-demo/"
        + ("Service" if change["kind"] == "service" else "Deployment")
        + "/"
        + change["name"]
    )
    try:
        current = private_current[key]
        audit.check(
            "fixture.root_resource_uid", change["uid"], current["metadata"]["uid"]
        )
        for kind, expected in (
            ("resources", change["injected"]),
            ("baseline", change["original"]),
        ):
            raw_item = next(
                item
                for item in records[kind]["data"]["resources"]
                if resource_key(item) == key
            )
            compact_item = next(
                item
                for item in compacts[kind]["data"]["resources"]
                if resource_key(item) == key
            )
            source = extract_repair_field(
                raw_item, change["path"], current, compact=False
            )
            observed = extract_repair_field(
                compact_item, change["path"], current, compact=True
            )
            audit.check("fixture.root_" + kind + "_matches_reference", expected, source)
            audit.check(
                "fixture.root_" + kind + "_value_locatable_in_compact",
                expected,
                observed,
            )
    except (KeyError, ValueError, TypeError, IndexError, StopIteration) as exc:
        audit.note(
            "fixture.root_field",
            "unverifiable",
            "Cannot resolve the recorded root field: " + type(exc).__name__,
        )


def audit_case(case, capture, records, fixture, private_current, compacts=None):
    audit = Audit()
    before = digest(records)
    compacts = compacts or {
        kind: view(record, "compact") for kind, record in records.items()
    }
    event_loss, raw_oom = {}, None
    for kind in KINDS:
        record, short = records[kind], compacts[kind]
        for field in ("id", "kind", "collected_at", "source", "content_hash"):
            audit.check(
                kind + ".record." + field,
                record.get(field, MISSING),
                short.get(field, MISSING),
            )
        raw, compact = record["data"], short["data"]
        if raw.get("collection_error"):
            audit.check(kind + ".collection_error_complete_record", record, short)
            audit.note(
                kind + ".actual_observations",
                "unverifiable",
                "采集失败；错误完整保留不等于缺失的日志或指标已恢复。",
            )
        elif kind in {"resources", "baseline"}:
            resources(audit, kind, raw, compact)
        elif kind == "observations":
            event_loss, raw_oom = observations(audit, raw, compact, case.target)
        elif kind == "releases":
            releases(audit, raw, compact, fixture, case)
        elif kind == "logs":
            for field in set(raw) - {"raw"}:
                audit.check("logs." + field, raw[field], compact.get(field, MISSING))
            if "raw" in raw:
                extracted = [
                    {"timestamp": stamp, "line": line}
                    for stream in raw["raw"].get("data", {}).get("result", [])
                    for stamp, line in stream.get("values", [])
                ]
                audit.check(
                    "logs.all_returned_line_contents_and_timestamps",
                    extracted,
                    compact.get("lines", MISSING),
                )
                audit.note(
                    "logs.raw_api_wrapper",
                    "intentional_omission",
                    "Duplicate raw Loki API envelope, stream labels and statistics are omitted; every fetched line and timestamp plus query/start are checked.",
                )
        else:
            audit.check("metrics.complete_response_query_and_error", raw, compact)
    fixture_root(audit, records, compacts, fixture, private_current)
    if case.family == "resources" and not raw_oom:
        audit.note(
            "runtime.resource_fault_oom_evidence",
            "unverifiable",
            "The raw capture has no current/last OOMKilled state for the target. An earlier fixture OOM observation is not a substitute for evidence present in the snapshot.",
        )
    audit.check("audit.raw_inputs_not_mutated", before, digest(records))
    return {
        "case_id": case.id,
        "split": case.split,
        "snapshot_id": capture["snapshot_id"],
        "checks": audit.checks,
        "events": event_loss,
        "target_oom_container_states_in_raw": raw_oom,
        "raw_serialized_bytes": len(json.dumps(records, ensure_ascii=False).encode()),
        "compact_serialized_bytes": len(
            json.dumps(compacts, ensure_ascii=False).encode()
        ),
        "byte_measurement_is_provider_tokens": False,
        "critical_lost": [
            row["name"]
            for row in audit.checks
            if row["status"] == "lost" and row["critical"]
        ],
    }


def run_audit():
    index = json.loads((PRIVATE / "capture-index.json").read_text())
    frozen = json.loads((PRIVATE / "frozen-snapshots.json").read_text())["captures"]
    results = []
    for case in (*DEV, *VALIDATION):
        capture = index[case.id]
        snapshot = WORK / "snapshots" / capture["snapshot_id"]
        records = load_records(snapshot)
        identity = {
            "snapshot_id": capture["snapshot_id"],
            "records_sha256": {
                kind: digest(record) for kind, record in records.items()
            },
            "incident_sha256": digest(
                json.loads((snapshot / "incident.json").read_text())
            ),
        }
        if identity != frozen[case.id]:
            raise ValueError("Frozen input integrity mismatch: " + case.id)
        folder = Path(capture["capture_folder"])
        fixture = json.loads((folder / "fixture.json").read_text())
        private_current = json.loads((folder / "current-raw.json").read_text())
        results.append(audit_case(case, capture, records, fixture, private_current))
    checks = [check for result in results for check in result["checks"]]
    counts = Counter(check["status"] for check in checks)
    lost = [
        {"case_id": result["case_id"], "check": name}
        for result in results
        for name in result["critical_lost"]
    ]
    return {
        "schema_version": 1,
        "generated_at": now(),
        "cases": results,
        "summary": {
            "snapshots_audited": len(results),
            "status_counts": dict(counts),
            "critical_lost_count": len(lost),
            "critical_lost": lost,
            "events_fetched": sum(
                result["events"].get("fetched", 0) for result in results
            ),
            "events_retained": sum(
                result["events"].get("retained", 0) for result in results
            ),
            "events_omitted": sum(
                result["events"].get("omitted", 0) for result in results
            ),
            "upstream_truncated_snapshots": sum(
                result["events"].get("upstream_truncated") is True for result in results
            ),
            "possibly_relevant_omitted_events": sum(
                result["events"].get("possibly_relevant_omitted_count", 0)
                for result in results
            ),
            "raw_serialized_bytes": sum(
                result["raw_serialized_bytes"] for result in results
            ),
            "compact_serialized_bytes": sum(
                result["compact_serialized_bytes"] for result in results
            ),
        },
        "all_raw_facts_lossless": False,
        "model_accuracy_evaluated": False,
        "provider_calls": 0,
        "fixture_answers_only_consulted_in_evaluator": True,
        "source_hashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "backend/cloudsre/experiment_views.py",
                "backend/cloudsre/evidence_v3.py",
                "benchmarks/d5/fact_audit.py",
            )
        },
        "limits": [
            "本审计只核20个冻结开发/验证快照的所选机械事实，不评实时调查正确性、所有可能诊断事实或D6留出集。",
            "较早事件、非业务Pod、Pod归属/标签及其他元数据被有意省略；计数和limit提示不能使其成为无损视图。",
            "采集错误保留只证明错误可见，不能证明不可用的日志或指标已经获取。",
            "发布时间线反映ReplicaSet创建时间，不是CI发布时间；只能核验实际已获取的版本。",
            "序列化字节减少不等于provider token或金额节省；后两者仍需实际usage与任务效果证据。",
        ],
    }


def markdown(report):
    summary = report["summary"]
    lines = [
        "# D5 紧凑证据关键事实审计",
        "",
        f"只读审计 {summary['snapshots_audited']} 个冻结快照；模型调用0次。所选关键字段丢失检查数：{summary['critical_lost_count']}。这不是模型正确率或全量无损声明。",
        "",
        f"逐项状态：{json.dumps(summary['status_counts'], ensure_ascii=False)}。",
        "",
        f"事件共读取 {summary['events_fetched']} 条，紧凑视图保留 {summary['events_retained']} 条、省略 {summary['events_omitted']} 条；{summary['upstream_truncated_snapshots']} 个快照上游事件查询已截断；省略中 {summary['possibly_relevant_omitted_events']} 条是按目标名称/警告类型筛出的可能相关事件，不能保证无关。",
        "",
        "| 案例 | 关键字段丢失 | 事件读取/保留/省略 | 上游截断 | 目标OOM状态数 | 原始/紧凑字节 |",
        "|---|---:|---|---|---:|---|",
    ]
    for result in report["cases"]:
        events = result["events"]
        lines.append(
            f"| {result['case_id']} | {len(result['critical_lost'])} | {events.get('fetched')}/{events.get('retained')}/{events.get('omitted')} | {events.get('upstream_truncated')} | {result['target_oom_container_states_in_raw']} | {result['raw_serialized_bytes']}/{result['compact_serialized_bytes']} |"
        )
    lines.extend(["", "## 丢失与不可验证事项", ""])
    for result in report["cases"]:
        for check in result["checks"]:
            if check["status"] in {"lost", "unverifiable"}:
                lines.append(
                    f"- {result['case_id']}：`{check['name']}` — {check['status']}；{check.get('reason', '原始值与紧凑值不同，见JSON中的哈希。')}"
                )
    lines.extend(["", "## 结论边界", ""])
    lines.extend("- " + limit for limit in report["limits"])
    lines.extend(
        [
            "",
            "完整逐字段保留/有意省略/不可验证记录、事件截断标志和原始/紧凑值哈希见同名 JSON。",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=WORK / "fact-audit.json")
    args = parser.parse_args()
    report = run_audit()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    args.output.with_suffix(".md").write_text(markdown(report))
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
