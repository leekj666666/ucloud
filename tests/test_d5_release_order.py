"""Misleading recency must exist in actual evidence, not just a fixture flag."""

import copy
import json
from types import SimpleNamespace

import pytest
from cloudsre.evidence_v3 import release_revisions

from benchmarks.d5 import diagnose, e2e
from benchmarks.d5.catalog import DEV
from benchmarks.d5.live import confirm_release_order


def test_newest_unrelated_release_is_verified_against_public_owner_and_value():
    case = next(row for row in DEV if row.id == "dev_09")
    fixture = SimpleNamespace(
        record={"changes": [{"injected": "r-controlled"}]}, persist=lambda: None
    )
    newest = release_revisions(
        [
            {
                "metadata": {
                    "name": "gateway-rs",
                    "namespace": "sre-demo",
                    "creationTimestamp": "2026-10-25T00:00:02Z",
                    "uid": "new-unrelated",
                    "resourceVersion": "1",
                    "ownerReferences": [
                        {
                            "kind": "Deployment",
                            "controller": True,
                            "name": case.distractor,
                            "uid": "dep-gateway",
                        }
                    ],
                },
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "gateway",
                                    "image": "demo:pinned",
                                    "env": [
                                        {"name": "RELEASE_ID", "value": "r-controlled"}
                                    ],
                                }
                            ]
                        }
                    }
                },
            }
        ]
    )[0]
    records = {"releases": {"data": {"release_revisions": [newest]}}}
    confirm_release_order(case, fixture, records)
    assert fixture.record["newest_unrelated_release_uid"] == "new-unrelated"
    for mutation in (
        "owner",
        "release_value",
        "newer_causal_release",
        "same_timestamp",
    ):
        changed = copy.deepcopy(records)
        rows = changed["releases"]["data"]["release_revisions"]
        if mutation == "owner":
            rows[-1]["owners"] = [{"name": case.target}]
        elif mutation == "release_value":
            rows[-1]["containers"][0]["env"]["RELEASE_ID"] = "old-release"
        else:
            causal = copy.deepcopy(newest)
            causal.update(
                created_at="2026-10-25T00:00:02Z"
                if mutation == "same_timestamp"
                else "2026-10-25T00:00:03Z",
                uid="aaa-causal" if mutation == "same_timestamp" else "newer-causal",
                owners=[{"name": case.target}],
            )
            rows.append(causal)
        with pytest.raises(ValueError, match="newest rollout|Timestamp tie"):
            confirm_release_order(case, fixture, changed)


def test_scoring_and_root_check_use_root_index_before_unrelated_release(
    tmp_path, monkeypatch
):
    case = next(row for row in DEV if row.id == "dev_09")
    record = {
        "root_change_index": 0,
        "changes": [
            {
                "kind": "deployment",
                "name": "order",
                "uid": "order-uid",
                "path": "/spec/url",
                "original": "http://inventory:8000",
                "injected": "http://absent:8000",
            },
            {
                "kind": "deployment",
                "name": "gateway",
                "uid": "gateway-uid",
                "path": "/spec/release",
                "original": "r-old",
                "injected": "r-new",
            },
        ],
    }
    (tmp_path / "fixture.json").write_text(json.dumps(record))
    (tmp_path / "current-raw.json").write_text(
        json.dumps({"sre-demo/Deployment/order": {}})
    )
    baseline = tmp_path / "infra/.generated/d4/baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("{}")
    monkeypatch.setattr(diagnose, "ROOT", tmp_path)
    monkeypatch.setattr(diagnose, "authorize", lambda *_: {"safe": True})
    proposal = {
        "action": "restore_env",
        "resource": "sre-demo/Deployment/order",
        "value": "http://inventory:8000",
        "reason": "actual dependency logs and baseline",
        "expected_spec_hash": "a" * 64,
        "evidence_ids": ["e-valid"],
    }
    result = diagnose.score(case, {"capture_folder": str(tmp_path)}, proposal, {})
    assert result["root_action_proxy_correct"] is True
    assert result["disposition_correct"] is True
    reads = []

    def read(kind, name):
        reads.append((kind, name))
        return {
            "metadata": {"uid": "order-uid"},
            "spec": {"url": "http://inventory:8000"},
        }

    monkeypatch.setattr(e2e, "read", read)
    assert e2e.root_state(SimpleNamespace(record=record))["passed"] is True
    assert reads == [("deployment", "order")]
