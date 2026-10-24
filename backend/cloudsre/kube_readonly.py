"""Fixed-path Kubernetes reads via the installed Python client; no shell subprocess."""

import json

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

PATHS = {
    ("deployment", "order"): "/apis/apps/v1/namespaces/sre-demo/deployments/order",
    ("replicaset", None): "/apis/apps/v1/namespaces/sre-demo/replicasets",
    ("service", "inventory"): "/api/v1/namespaces/sre-demo/services/inventory",
    (
        "endpointslice",
        None,
    ): "/apis/discovery.k8s.io/v1/namespaces/sre-demo/endpointslices",
}


def read(resource, name=None):
    path = PATHS.get((resource, name))
    if path is None:
        raise ValueError("Unsupported read resource")
    try:
        with config.new_client_from_config(
            context="kind-sre-dev", persist_config=False
        ) as api:
            if resource == "deployment":
                fn = client.AppsV1Api(api).read_namespaced_deployment
                args = {"name": "order", "namespace": "sre-demo"}
            elif resource == "replicaset":
                fn = client.AppsV1Api(api).list_namespaced_replica_set
                args = {"namespace": "sre-demo"}
            elif resource == "service":
                fn = client.CoreV1Api(api).read_namespaced_service
                args = {"name": "inventory", "namespace": "sre-demo"}
            else:
                fn = client.DiscoveryV1Api(api).list_namespaced_endpoint_slice
                args = {"namespace": "sre-demo"}
            response = fn(**args, _preload_content=False, _request_timeout=(5, 15))
            try:
                if response.status != 200:
                    raise RuntimeError(
                        "Kubernetes read returned HTTP " + str(response.status)
                    )
                return json.loads(response.read())
            finally:
                response.release_conn()
    except (ApiException, HTTPError) as exc:
        raise RuntimeError(
            "Kubernetes read unavailable: " + type(exc).__name__
        ) from None


def inventory_snapshot():
    service = read("service", "inventory")
    slices = read("endpointslice")["items"]
    return {
        "service": "inventory",
        "namespace": "sre-demo",
        "ports": [p["port"] for p in service["spec"]["ports"]],
        "ready_endpoints": sum(
            1
            for s in slices
            if s["metadata"].get("labels", {}).get("kubernetes.io/service-name")
            == "inventory"
            for e in s.get("endpoints", [])
            if e.get("conditions", {}).get("ready")
        ),
    }
