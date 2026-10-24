import json
from unittest.mock import MagicMock

import pytest
from cloudsre import kube_readonly as kube


@pytest.mark.parametrize(
    "resource,name",
    [
        ("secret", None),
        ("deployment", "inventory"),
        ("service", "order"),
        ("replicaset", "order"),
    ],
)
def test_read_rejects_unapproved_target_before_client(monkeypatch, resource, name):
    factory = MagicMock()
    monkeypatch.setattr(kube.config, "new_client_from_config", factory)
    with pytest.raises(ValueError):
        kube.read(resource, name)
    factory.assert_not_called()


@pytest.mark.parametrize("status,body", [(403, b"{}"), (200, b"not-json")])
def test_read_fails_closed_and_releases_connection(monkeypatch, status, body):
    factory = MagicMock()
    monkeypatch.setattr(kube.config, "new_client_from_config", factory)
    response = MagicMock(status=status)
    response.read.return_value = body
    api = MagicMock()
    api.read_namespaced_deployment.return_value = response
    monkeypatch.setattr(kube.client, "AppsV1Api", lambda _: api)
    with pytest.raises((RuntimeError, json.JSONDecodeError)):
        kube.read("deployment", "order")
    response.release_conn.assert_called_once()
    args = api.read_namespaced_deployment.call_args.kwargs
    assert args["namespace"] == "sre-demo" and args["name"] == "order"
    assert args["_request_timeout"] == (5, 15)
