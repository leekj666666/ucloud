import pytest
from cloudsre.resource_metrics import oom_services


@pytest.mark.parametrize(
    ("ready", "reason", "deleted", "expected"),
    [
        (False, "OOMKilled", False, 1),
        (True, "OOMKilled", False, 0),
        (False, "Error", False, 0),
        (False, "OOMKilled", True, 0),
    ],
)
def test_resource_alert_only_tracks_active_oom(ready, reason, deleted, expected):
    pod = {
        "metadata": {"labels": {"app": "inventory"}},
        "status": {
            "containerStatuses": [
                {"ready": ready, "lastState": {"terminated": {"reason": reason}}}
            ]
        },
    }
    if deleted:
        pod["metadata"]["deletionTimestamp"] = "2026-10-24T00:00:00Z"
    assert oom_services([pod])["inventory"] == expected
