"""Isolated browser contract fixture. No model, Kubernetes or business credentials.

All seeded results are test data, never evidence of a real business recovery.
"""

import argparse
import json
import tempfile
import uuid
from pathlib import Path

import uvicorn
from cloudsre.app import create_app
from cloudsre.controls import Controls
from cloudsre.domain import Status
from cloudsre.durable import now
from cloudsre.store import Store

ROOT = Path(__file__).resolve().parents[1]


def seed(folder):
    url = "sqlite:///" + str(folder / "browser.db")
    store = Store(url)
    store.initialize()
    controls = Controls(store)
    for label in (
        name + " · " + project
        for project in ("desktop", "mobile")
        for name in ("批准测试", "拒绝测试", "断连测试", "冲突测试", "文本与导出测试")
    ):
        incident, _ = controls.create(
            title="浏览器测试夹具 · " + label,
            service="order",
            namespace="sre-demo",
            execution_mode="manual",
            request_id=uuid.uuid4().hex,
            actor="browser-fixture",
        )
        ident, run_id, action_id = incident["id"], uuid.uuid4().hex, uuid.uuid4().hex
        intent = {
            "id": action_id,
            "action": "restore_env",
            "resource": "sre-demo/Deployment/order",
            "value": "http://inventory:8000",
            "before": {
                "uid": "fixture-uid",
                "resource_version": "42",
                "spec_hash": "fixture-hash",
            },
            "patch": [
                {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
                {
                    "op": "test",
                    "path": "/spec/template/spec/containers/0/env/0/value",
                    "value": "http://broken:8000",
                },
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/env/0/value",
                    "value": "http://inventory:8000",
                },
            ],
        }
        for old, new in (
            (Status.NEW, Status.INVESTIGATING),
            (Status.INVESTIGATING, Status.PLAN_READY),
            (Status.PLAN_READY, Status.WAITING_APPROVAL),
        ):
            store.transition(ident, old, new)
        (folder / run_id / "evidence").mkdir(parents=True)
        evidence = {
            "id": "e-" + uuid.uuid4().hex[:8],
            "kind": "logs",
            "sha256": "fixture",
            "content": "<script>window.fixtureXss=1</script> 原始日志文本",
        }
        (folder / run_id / "evidence" / (evidence["id"] + ".json")).write_text(
            json.dumps(evidence)
        )
        store.save_run(
            run_id,
            ident,
            "WAITING_APPROVAL",
            {
                "schema_version": 3,
                "run_id": run_id,
                "namespace": "sre-demo",
                "started_at": now(),
                "action_intent": intent,
                "outcome": "awaiting_approval",
                "proposal": {
                    "reason": "<script>window.fixtureXss=1</script> 仅用于浏览器契约测试的判断",
                    "evidence_ids": [evidence["id"]],
                },
                "events": [],
            },
        )
    store.engine.dispose()
    return url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18089)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cloudsre-browser-") as directory:
        folder = Path(directory)
        app = create_app(
            seed(folder),
            console_token="browser-contract-access",
            evidence_roots=[folder],
            static_dir=ROOT / "frontend/dist",
        )
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
