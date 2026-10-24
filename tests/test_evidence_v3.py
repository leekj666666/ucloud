import copy

from cloudsre.evidence_v3 import model_view, release_revisions
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
            "release_revisions": [{"uid": "release-id", "created_at": "observed-time"}],
            "event_query": {"limit": 50, "truncated": True},
        },
    }
    compact = model_view(record)
    assert compact["data"]["event_query"]["truncated"] is True
    assert compact["data"]["release_revisions"] == record["data"]["release_revisions"]
    assert "event_view_limit" not in record["data"]
