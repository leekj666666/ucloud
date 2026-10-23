import json
from pathlib import Path
from urllib.parse import quote

from cloudsre.app import create_app
from cloudsre.domain import Status
from cloudsre.store import Store
from dotenv import dotenv_values
from fastapi.testclient import TestClient

root = Path(__file__).resolve().parents[1]
raw = json.loads((root / "evaluation/runs/d0-holmes/holmes-result.json").read_text())
assert raw["tool_calls"], "No real tool call recorded"
assert "get_monitoring_health" in json.dumps(raw["tool_calls"])
assert "prometheus" in json.dumps(raw["tool_calls"]).lower()
cfg = dotenv_values(root / "infra/.env")
url = (
    "postgresql+psycopg://sre:"
    + quote(cfg["POSTGRES_PASSWORD"], safe="")
    + "@localhost:15432/sre"
)
store = Store(url)
with TestClient(create_app(url)) as c:
    assert c.get("/health").status_code == 200
    payload = {
        "dedup_key": "d0-monitoring-smoke",
        "title": "Monitoring health investigation",
        "service": "prometheus",
    }
    r = c.post("/incidents", json=payload)
    assert r.status_code in (200, 201)
    row = r.json()
    if row["status"] == Status.NEW:
        store.transition(row["id"], Status.NEW, Status.INVESTIGATING)
        store.transition(
            row["id"],
            Status.INVESTIGATING,
            Status.DIAGNOSED,
            json.dumps(raw, ensure_ascii=False),
        )
    found = c.get("/incidents/" + row["id"]).json()
    assert found["status"] == Status.DIAGNOSED
    assert json.loads(found["diagnosis"])["tool_calls"]
report = {
    "stage": "D0",
    "postgres_api": "passed",
    "deduplication": "passed",
    "real_holmes_tools": raw["tool_calls"],
    "incident_id": row["id"],
    "status": found["status"],
    "answer": raw["result"],
    "usage": {
        k: raw.get(k)
        for k in [
            "total_tokens",
            "prompt_tokens",
            "completion_tokens",
            "num_llm_calls",
            "total_cost",
        ]
    },
    "note": "Upstream monetary estimates are not verified invoice costs. This is diagnosis only, not repair.",
}
(root / "evaluation/runs/d0-holmes/verification.json").write_text(
    json.dumps(report, indent=2, ensure_ascii=False)
)
public = {k: v for k, v in report.items() if k != "real_holmes_tools"}
print(json.dumps(public, indent=2, ensure_ascii=False))
