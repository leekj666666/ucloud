import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .domain import IncidentInput


class Alert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime | None = None


class AlertBatch(BaseModel):
    alerts: list[Alert] = Field(min_length=1, max_length=100)


def ingest(store, batch):
    ids = []
    for alert in batch.alerts:
        identity = json.dumps(
            {"labels": alert.labels, "startsAt": alert.startsAt.isoformat()},
            sort_keys=True,
        )
        key = "alert:" + hashlib.sha256(identity.encode()).hexdigest()
        service = alert.labels.get("service", "unknown")[:100]
        name = alert.labels.get("alertname", "UnnamedAlert")
        row, _ = store.create(
            IncidentInput(
                dedup_key=key, title=f"{name}: {service}"[:300], service=service
            )
        )
        store.record_alert(row["id"], key, alert.status, alert.model_dump(mode="json"))
        ids.append(row["id"])
    return {"incident_ids": ids}
