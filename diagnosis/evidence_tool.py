"""Fixed read-only tool commands; the model cannot choose paths, URLs or commands."""

import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from cloudsre.kube import deployment_snapshot, inventory_snapshot, read


def collect(kind):
    if kind == "deployment":
        current = deployment_snapshot(read("deployment", "order"))
        replicas = read("replicaset")["items"]
        history = []
        for r in replicas:
            if not any(
                x.get("uid") == current["uid"]
                for x in r["metadata"].get("ownerReferences", [])
            ):
                continue
            c = next(
                c
                for c in r["spec"]["template"]["spec"]["containers"]
                if c["name"] == "order"
            )
            if (
                os.environ.get("CLOUDSRE_EVIDENCE_SINCE")
                and r["metadata"]["creationTimestamp"]
                < os.environ["CLOUDSRE_EVIDENCE_SINCE"]
            ):
                continue
            history.append(
                {
                    "revision": r["metadata"]
                    .get("annotations", {})
                    .get("deployment.kubernetes.io/revision"),
                    "created_at": r["metadata"]["creationTimestamp"],
                    "image": c["image"],
                    "image_content_id": r["spec"]["template"]
                    .get("metadata", {})
                    .get("annotations", {})
                    .get("cloudsre/image-id"),
                    "env": {
                        e["name"]: e.get("value")
                        for e in c.get("env", [])
                        if e["name"] in {"INVENTORY_URL", "RELEASE_ID"}
                    },
                }
            )
        return {"current": current, "history": history}
    if kind == "inventory":
        return inventory_snapshot()
    if kind == "metrics":
        expr = (
            'sum by(service,status)(rate(sre_http_requests_total{route="/orders"}[1m]))'
        )
        r = httpx.get(
            "http://localhost:19090/api/v1/query", params={"query": expr}, timeout=10
        )
        r.raise_for_status()
        return {"query": expr, "response": r.json()}
    if kind == "logs":
        query = '{namespace="sre-demo",service="order"} | json | event="inventory_unavailable"'
        r = httpx.get(
            "http://localhost:13100/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": 24,
                **(
                    {
                        "start": str(
                            int(
                                datetime.fromisoformat(
                                    os.environ["CLOUDSRE_EVIDENCE_SINCE"]
                                ).timestamp()
                                * 1e9
                            )
                        )
                    }
                    if os.environ.get("CLOUDSRE_EVIDENCE_SINCE")
                    else {"since": "5m"}
                ),
            },
            timeout=10,
        )
        r.raise_for_status()
        return {"query": query, "response": r.json()}
    raise ValueError("Unknown evidence tool")


if __name__ == "__main__":
    kind = sys.argv[1]
    data = collect(kind)
    ident = "e-" + uuid.uuid4().hex[:16]
    record = {
        "id": ident,
        "kind": kind,
        "collected_at": datetime.now(UTC).isoformat(),
        "data": data,
        "content_hash": hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest(),
        "source": "live-read-only-tool",
    }
    folder = Path(os.environ["CLOUDSRE_RUN_DIR"]) / "evidence"
    folder.mkdir(exist_ok=True, parents=True)
    (folder / (ident + ".json")).write_text(json.dumps(record, ensure_ascii=False))
    print(json.dumps(record, ensure_ascii=False))
