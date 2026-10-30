import copy
import json

import pytest
from cloudsre.evidence_v3 import public_resource
from cloudsre.experiment_views import KINDS, view

from benchmarks.d5.catalog import DEV
from benchmarks.d5.fact_audit import (
    Audit,
    audit_case,
    fixture_root,
    observations,
    releases,
    resources,
)


def record(kind, data):
    return {
        "id": "e-" + kind,
        "kind": kind,
        "content_hash": "test-record",
        "data": data,
    }


def deployment(url):
    return {
        "kind": "Deployment",
        "metadata": {
            "name": "order",
            "namespace": "sre-demo",
            "uid": "deployment-order",
            "resourceVersion": "123",
            "generation": 2,
            "creationTimestamp": "2026-10-25T00:00:00Z",
        },
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "order",
                            "image": "demo:fixed",
                            "env": [
                                {
                                    "name": "DATABASE_URL",
                                    "value": "private-database-url-must-never-be-output",
                                },
                                {"name": "INVENTORY_URL", "value": url},
                            ],
                            "resources": {"limits": {"memory": "96Mi"}},
                            "ports": [{"containerPort": 8000}],
                        }
                    ]
                }
            },
        },
        "status": {"readyReplicas": 1},
    }


def service():
    return {
        "identity": {
            "resource": "sre-demo/Service/order",
            "uid": "service-order",
            "spec_hash": "spec",
            "resource_version": "456",
        },
        "created_at": "2026-10-25T00:00:00Z",
        "annotations": {},
        "spec": {
            "ports": [{"port": 8000, "targetPort": 8000}],
            "selector": {"app": "order"},
            "type": "ClusterIP",
        },
    }


def runtime_data():
    return {
        "pods": [
            {
                "name": "inventory-failed",
                "labels": {"app": "inventory"},
                "owners": [{"uid": "rs-current"}],
                "containers": [
                    {"name": "inventory", "resources": {"limits": {"memory": "8Mi"}}}
                ],
                "status": {
                    "phase": "Running",
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "False",
                            "reason": "ContainersNotReady",
                        }
                    ],
                    "containerStatuses": [
                        {
                            "name": "inventory",
                            "ready": False,
                            "restartCount": 3,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                            "lastState": {
                                "terminated": {"reason": "OOMKilled", "exitCode": 137}
                            },
                        }
                    ],
                },
            }
        ],
        "events": [
            {
                "metadata": {
                    "creationTimestamp": "2026-10-25T00:00:" + str(index).zfill(2) + "Z"
                },
                "involvedObject": {"name": "inventory-failed"},
                "type": "Warning",
                "reason": "BackOff",
                "message": "memory fault " + str(index),
                "count": 1,
            }
            for index in range(14)
        ],
        "event_query": {"limit": 50, "truncated": True},
        "endpoints": [
            {
                "metadata": {"labels": {"kubernetes.io/service-name": "inventory"}},
                "ports": [{"port": 8000}],
                "endpoints": [
                    {"addresses": ["10.0.0.8"], "conditions": {"ready": False}}
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "field", ["selector", "targetPort", "identity", "replicas", "env", "resources"]
)
def test_selected_resource_field_audit_detects_real_loss(field):
    raw = {
        "resources": [public_resource(deployment("http://inventory:8000")), service()]
    }
    compact = view(record("resources", raw), "compact")["data"]
    deployment_view, service_view = compact["resources"]
    if field == "selector":
        service_view["spec"].pop("selector")
    elif field == "targetPort":
        service_view["spec"]["ports"][0]["targetPort"] = 9999
    elif field == "identity":
        deployment_view["identity"]["spec_hash"] = "invented"
    elif field == "replicas":
        deployment_view["spec"]["replicas"] = 0
    else:
        deployment_view["spec"]["containers"][0].pop(field)
    audit = Audit()
    resources(audit, "resources", raw, compact)
    assert any(
        check["status"] == "lost" and check["critical"] for check in audit.checks
    )


def test_runtime_audit_keeps_event_loss_and_truncation_visible():
    raw = runtime_data()
    compact = view(record("observations", raw), "compact")["data"]
    audit = Audit()
    event_loss, oom = observations(audit, raw, compact, "inventory")
    assert oom == 1
    assert (event_loss["fetched"], event_loss["retained"], event_loss["omitted"]) == (
        14,
        12,
        2,
    )
    assert event_loss["upstream_truncated"] is True
    assert event_loss["possibly_relevant_omitted_count"] == 2
    assert event_loss["explicit_view_truncated_boolean_present"] is False
    assert event_loss["view_omission_indicated_by_count_and_limit"] is True
    assert any(
        check["name"] == "events.older_payload_loss"
        and check["status"] == "intentional_omission"
        for check in audit.checks
    )
    assert not any(check["status"] == "lost" for check in audit.checks)


@pytest.mark.parametrize("field", ["OOMKilled", "readiness", "endpoint_address"])
def test_runtime_audit_detects_oom_readiness_or_endpoint_loss(field):
    raw = runtime_data()
    compact = view(record("observations", raw), "compact")["data"]
    if field == "OOMKilled":
        compact["pods"][0]["container_states"][0]["lastState"] = {}
    elif field == "readiness":
        compact["pods"][0]["conditions"][0]["status"] = "True"
    else:
        compact["endpoints"][0]["endpoints"][0]["addresses"] = ["10.0.0.99"]
    audit = Audit()
    observations(audit, raw, compact, "inventory")
    assert any(
        check["status"] == "lost" and check["critical"] for check in audit.checks
    )


def release_data():
    return {
        "release_revisions": [
            {
                "uid": "rs-" + str(index),
                "created_at": "2026-10-25T00:00:0" + str(index) + "Z",
                "revision": str(index),
                "owners": [{"name": "gateway", "uid": "deployment-gateway"}],
                "containers": [
                    {
                        "name": "gateway",
                        "image": "demo:fixed",
                        "env": {
                            "RELEASE_ID": release_id,
                            "ORDER_URL": "http://order:8000",
                        },
                    }
                ],
                "image_content_id": "fixed-image-digest",
            }
            for index, release_id in enumerate(("old-release", "latest-unrelated"))
        ]
    }


def test_release_audit_decodes_uid_owner_latest_and_configuration_changes():
    raw = release_data()
    compact = view(record("releases", raw), "compact")["data"]
    fixture = {
        "newest_unrelated_release_uid": "rs-1",
        "changes": [{"name": "gateway", "injected": "latest-unrelated"}],
    }
    audit = Audit()
    releases(audit, raw, compact, fixture, DEV[8])
    assert not any(
        check["status"] in {"lost", "unverifiable"} for check in audit.checks
    )
    compact["release_timeline"]["groups"][0]["configurations"][1]["containers"][0][
        "env"
    ]["RELEASE_ID"] = "old-release"
    audit = Audit()
    releases(audit, raw, compact, fixture, DEV[8])
    losses = {check["name"] for check in audit.checks if check["status"] == "lost"}
    assert "releases.all_adjacent_configuration_differences" in losses
    assert "releases.fixture_unrelated_release_id_locatable" in losses


def test_collection_error_is_checked_in_full_without_inventing_observations():
    records = {
        kind: record(kind, {"collection_error": True, "error_type": "ConnectionError"})
        for kind in KINDS
    }
    original = copy.deepcopy(records)
    result = audit_case(
        DEV[0], {"snapshot_id": "snapshot"}, records, {"changes": []}, {}
    )
    assert records == original
    assert not result["critical_lost"]
    assert (
        sum(
            check["status"] == "unverifiable" and "actual_observations" in check["name"]
            for check in result["checks"]
        )
        == 6
    )
    compact = {kind: view(value, "compact") for kind, value in records.items()}
    compact["logs"]["data"]["error_type"] = "InventedSuccess"
    result = audit_case(
        DEV[0],
        {"snapshot_id": "snapshot"},
        records,
        {"changes": []},
        {},
        compacts=compact,
    )
    assert "logs.collection_error_complete_record" in result["critical_lost"]


def test_fixture_root_uses_container_env_name_after_sanitization_not_old_index():
    current = deployment("http://inventory.invalid:8000")
    baseline = deployment("http://inventory:8000")
    records = {
        "resources": record("resources", {"resources": [public_resource(current)]}),
        "baseline": record("baseline", {"resources": [public_resource(baseline)]}),
    }
    compact = {kind: view(value, "compact") for kind, value in records.items()}
    change = {
        "kind": "deployment",
        "name": "order",
        "uid": "deployment-order",
        "path": "/spec/template/spec/containers/0/env/1/value",
        "original": "http://inventory:8000",
        "injected": "http://inventory.invalid:8000",
    }
    fixture = {"root_change_index": 0, "changes": [change]}
    audit = Audit()
    fixture_root(
        audit, records, compact, fixture, {"sre-demo/Deployment/order": current}
    )
    assert len(audit.checks) == 5 and all(
        check["status"] == "preserved" for check in audit.checks
    )
    rendered = json.dumps(audit.checks)
    assert (
        "private-database-url" not in rendered
        and "http://inventory.invalid" not in rendered
    )
    fixture = {"changes": [change, {"name": "gateway", "injected": "unrelated"}]}
    audit = Audit()
    fixture_root(
        audit, records, compact, fixture, {"sre-demo/Deployment/order": current}
    )
    assert len(audit.checks) == 1 and audit.checks[0]["status"] == "unverifiable"
