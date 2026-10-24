"""Kubernetes API transport with fixed namespaces/resources and separate credentials."""

import json
import os

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from .actions import NAMESPACES, TARGETS


class KubeGateway:
    def __init__(self, role="read"):
        if role not in {"read", "execute"}:
            raise ValueError("Unknown Kubernetes identity")
        path = os.environ.get("CLOUDSRE_" + role.upper() + "_KUBECONFIG")
        if not path:
            raise ValueError("Separate scoped kubeconfig required")
        self.api = config.new_client_from_config(config_file=path, persist_config=False)
        self.role = role

    def close(self):
        self.api.close()

    def _target(self, key):
        ns, kind, name = key.split("/")
        if ns not in NAMESPACES or kind not in TARGETS or name not in TARGETS[kind]:
            raise ValueError("Unapproved Kubernetes target")
        service = (
            client.AppsV1Api(self.api)
            if kind == "Deployment"
            else client.CoreV1Api(self.api)
        )
        suffix = (
            "namespaced_deployment" if kind == "Deployment" else "namespaced_service"
        )
        return ns, name, service, suffix

    def _call(self, fn, **args):
        try:
            response = fn(**args, _preload_content=False, _request_timeout=(5, 15))
            try:
                if response.status not in {200, 201}:
                    raise RuntimeError("Kubernetes HTTP " + str(response.status))
                return json.loads(response.read())
            finally:
                response.release_conn()
        except (ApiException, HTTPError) as exc:
            raise RuntimeError(
                "Kubernetes request failed: " + type(exc).__name__
            ) from None

    def read(self, key):
        ns, name, api, suffix = self._target(key)
        return self._call(getattr(api, "read_" + suffix), name=name, namespace=ns)

    def patch(self, key, patch):
        if self.role != "execute":
            raise ValueError("Read identity cannot mutate")
        ns, name, api, suffix = self._target(key)
        return self._call(
            getattr(api, "patch_" + suffix),
            name=name,
            namespace=ns,
            body=patch,
            _content_type="application/json-patch+json",
        )

    def observations(self, namespace):
        if namespace not in NAMESPACES:
            raise ValueError("Unapproved namespace")
        core = client.CoreV1Api(self.api)
        events = self._call(core.list_namespaced_event, namespace=namespace, limit=50)
        return {
            "event_query": {
                "limit": 50,
                "truncated": bool(events.get("metadata", {}).get("continue")),
            },
            "pods": self._call(core.list_namespaced_pod, namespace=namespace)["items"],
            "endpoints": self._call(
                client.DiscoveryV1Api(self.api).list_namespaced_endpoint_slice,
                namespace=namespace,
            )["items"],
            "events": events["items"],
        }

    def releases(self, namespace):
        if namespace not in NAMESPACES:
            raise ValueError("Unapproved namespace")
        return self._call(
            client.AppsV1Api(self.api).list_namespaced_replica_set,
            namespace=namespace,
        )["items"]
