import copy

from cloudsre.evidence_v3 import compact_release_timeline, model_view, release_revisions
from test_actions_v3 import resource


def test_release_timeline_preserves_identity_and_redacts_private_environment():
    raw = resource(name="order")
    raw["metadata"].update(
        creationTimestamp="2026-10-25T01:00:00Z",
        ownerReferences=[
            {"kind": "Deployment", "name": "order", "uid": "owner", "controller": True}
        ],
    )
    raw["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "DATABASE_URL", "value": "private-value"}
    )
    unrelated = copy.deepcopy(raw)
    unrelated["metadata"]["ownerReferences"][0]["name"] = "unapproved"
    revisions = release_revisions([raw, unrelated])
    assert len(revisions) == 1
    assert revisions[0]["created_at"] == "2026-10-25T01:00:00Z"
    assert revisions[0]["owners"] == [{"name": "order", "uid": "owner"}]
    assert "not CI publication time" in revisions[0]["timestamp_semantics"]
    assert "private-value" not in str(revisions)


def test_compaction_keeps_release_timeline_and_query_truncation():
    record = {
        "kind": "observations",
        "data": {
            "pods": [],
            "events": [],
            "endpoints": [],
            "release_revisions": [
                {
                    "uid": "release-id",
                    "created_at": "observed-time",
                    "owners": [{"name": "order", "uid": "owner"}],
                    "containers": [],
                }
            ],
            "event_query": {"limit": 50, "truncated": True},
        },
    }
    compact = model_view(record)
    assert compact["data"]["event_query"]["truncated"] is True
    timeline = compact["data"]["release_timeline"]
    assert timeline["revision_count"] == 1
    assert timeline["groups"][0]["revisions"][0][:2] == ["release-id", "observed-time"]
    assert "event_view_limit" not in record["data"]


def test_release_compaction_keeps_all_revisions_and_exact_configuration_values():
    revisions = [
        {
            "uid": str(i),
            "created_at": str(i),
            "revision": str(i),
            "owners": [{"name": "order", "uid": "owner"}],
            "containers": [{"image": "immutable", "env": {"RELEASE_ID": str(i % 2)}}],
        }
        for i in range(40)
    ]
    timeline = compact_release_timeline(revisions)
    group = timeline["groups"][0]
    assert len(group["configurations"]) == 2
    assert len(group["revisions"]) == timeline["revision_count"] == 40
    for row, original in zip(group["revisions"], revisions, strict=True):
        assert row[:3] == [
            original["uid"],
            original["created_at"],
            original["revision"],
        ]
        assert group["configurations"][row[3]]["containers"] == original["containers"]
