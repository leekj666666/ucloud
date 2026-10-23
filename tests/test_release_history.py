import copy
import json

import pytest
from cloudsre.release_history import digest, record


def deployment(url="http://inventory:8000"):
    return {
        "metadata": {
            "name": "order",
            "namespace": "sre-demo",
            "uid": "uid",
            "resourceVersion": "1",
            "generation": 1,
            "creationTimestamp": "2026-10-23T00:00:00Z",
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "order",
                            "image": "image@sha256:abc",
                            "env": [
                                {"name": "INVENTORY_URL", "value": url},
                                {
                                    "name": "DATABASE_URL",
                                    "value": "do-not-export-this-secret",
                                },
                            ],
                        }
                    ]
                }
            }
        },
    }


def test_release_records_are_sanitized_hashed_and_linked(tmp_path):
    raw = deployment()
    raw["spec"]["template"]["metadata"] = {
        "annotations": {"cloudsre/image-id": "sha256:immutable-image-id"}
    }
    first = record(
        raw,
        tmp_path,
        healthy=True,
        verification_ref="verifications/checked.json",
    )
    assert first["snapshot"]["image_content_id"] == "sha256:immutable-image-id"
    assert len(first["snapshot"]["public_config_hash"]) == 64
    second = record(deployment("http://svc-123:8000"), tmp_path)
    assert (
        first["snapshot"]["public_config_hash"]
        != second["snapshot"]["public_config_hash"]
    )
    assert second["diff"]["image_content_id"]["before"] == "sha256:immutable-image-id"
    assert second["previous_hash"] == first["content_hash"]
    assert second["diff"]["order.INVENTORY_URL"]["after"] == "http://svc-123:8000"
    for path in tmp_path.glob("*.json"):
        assert "do-not-export-this-secret" not in path.read_text()
    changed = copy.deepcopy(second)
    assert changed.pop("content_hash") == digest(changed)
    changed["snapshot"]["uid"] = "another"
    assert second["content_hash"] != digest(changed)
    assert json.dumps(first)


def test_healthy_release_cannot_be_registered_without_verification(tmp_path):
    with pytest.raises(ValueError):
        record(deployment(), tmp_path, healthy=True)
    assert not list(tmp_path.glob("*.json"))
