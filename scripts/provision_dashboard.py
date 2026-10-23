import json
from pathlib import Path

import httpx
from dotenv import dotenv_values

root = Path.home() / "sre-project"
cfg = dotenv_values(root / "infra/.env")
with httpx.Client(
    base_url="http://localhost:13000",
    auth=("admin", cfg["GRAFANA_PASSWORD"]),
    timeout=15,
) as c:
    r = c.get("/api/datasources")
    r.raise_for_status()
    sources = {s["uid"] for s in r.json()}
    assert {"prometheus", "loki"} <= sources
    dashboard = {
        "uid": "cloudsre-demo",
        "title": "Cloud SRE — Demo Business",
        "schemaVersion": 39,
        "refresh": "5s",
        "time": {"from": "now-15m", "to": "now"},
        "panels": [
            {
                "id": 1,
                "title": "Order request rate",
                "type": "timeseries",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "targets": [
                    {
                        "refId": "A",
                        "expr": 'sum by (status) (rate(sre_http_requests_total{service="order",route="/orders"}[1m]))',
                        "legendFormat": "{{status}}",
                    }
                ],
            },
            {
                "id": 2,
                "title": "Order error rate",
                "type": "timeseries",
                "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "targets": [
                    {
                        "refId": "A",
                        "expr": 'sum(rate(sre_http_requests_total{service="order",status=~"5.."}[1m])) or vector(0)',
                    }
                ],
            },
            {
                "id": 3,
                "title": "Gateway, order, inventory and probe logs",
                "type": "logs",
                "gridPos": {"x": 0, "y": 8, "w": 24, "h": 12},
                "datasource": {"type": "loki", "uid": "loki"},
                "targets": [
                    {
                        "refId": "A",
                        "expr": '{namespace="sre-demo",service=~"gateway|order|inventory|probe"} | json',
                    }
                ],
            },
        ],
    }
    dashboard["templating"] = {
        "list": [
            {
                "name": "trace_id",
                "type": "textbox",
                "label": "Trace ID",
                "query": "",
                "current": {"text": "", "value": ""},
            }
        ]
    }
    dashboard["panels"].extend(
        [
            {
                "id": 4,
                "title": "Business probe success",
                "type": "timeseries",
                "gridPos": {"x": 0, "y": 20, "w": 12, "h": 8},
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "targets": [{"refId": "A", "expr": "sre_business_probe_success"}],
            },
            {
                "id": 5,
                "title": "Service target health",
                "type": "timeseries",
                "gridPos": {"x": 12, "y": 20, "w": 12, "h": 8},
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "targets": [
                    {
                        "refId": "A",
                        "expr": 'up{job="demo"}',
                        "legendFormat": "{{service}}",
                    }
                ],
            },
            {
                "id": 6,
                "title": "OpenTelemetry spans (filter by Trace ID)",
                "type": "logs",
                "gridPos": {"x": 0, "y": 28, "w": 24, "h": 12},
                "datasource": {"type": "loki", "uid": "loki"},
                "targets": [
                    {
                        "refId": "A",
                        "expr": '{namespace="sre-demo"} | json | event="otel_span" |= "$trace_id"',
                    }
                ],
            },
        ]
    )
    r = c.post("/api/dashboards/db", json={"dashboard": dashboard, "overwrite": True})
    r.raise_for_status()
    assert c.get("/api/dashboards/uid/cloudsre-demo").status_code == 200
    (root / "deploy/grafana-dashboard.json").write_text(json.dumps(dashboard, indent=2))
    print("Grafana data sources and demo dashboard verified:", r.json()["url"])
