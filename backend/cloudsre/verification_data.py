"""Evaluator-owned fixtures. No cleanup/reset endpoints are exposed to the Agent."""

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session


def verification_database_url():
    if os.environ.get("CLOUDSRE_VERIFY_DATABASE_URL"):
        return os.environ["CLOUDSRE_VERIFY_DATABASE_URL"]
    root = Path(__file__).resolve().parents[2]
    cfg = dotenv_values(root / "infra/.env.demo")
    if not cfg.get("DEMO_PASSWORD"):
        raise RuntimeError("Verification fixture database is not configured")
    return (
        "postgresql+psycopg://sre_demo:"
        + quote(cfg["DEMO_PASSWORD"], safe="")
        + "@localhost:15432/sre_demo"
    )


class VerificationData:
    def __init__(self, database_url=None, evidence_dir=None):
        self.engine = create_engine(database_url or verification_database_url())
        self.tenant = "verify-" + uuid.uuid4().hex
        self.sku = self.tenant + "-sku"
        self.evidence_dir = Path(
            evidence_dir
            or os.environ.get(
                "CLOUDSRE_VERIFY_EVIDENCE_DIR", "/tmp/cloudsre-verifications"
            )
        )

    def create(self):
        from .demo import Stock, Tenant

        with Session(self.engine) as s, s.begin():
            s.add(Tenant(id=self.tenant, purpose="verification"))
            s.flush()
            s.add(Stock(sku=self.sku, available=100, tenant_id=self.tenant))

    def archive(self, result):
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / (self.tenant + ".json")
        content = {
            "tenant": self.tenant,
            "sku": self.sku,
            "recorded_at": datetime.now(UTC).isoformat(),
            "result": result,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2))
        temporary.replace(path)
        return str(path)

    def cleanup(self):
        from .demo import Order, Reservation, Stock, Tenant

        if not self.tenant.startswith("verify-") or self.sku != self.tenant + "-sku":
            raise ValueError("Cleanup outside owned verification tenant refused")
        with Session(self.engine) as s, s.begin():
            tenant = s.scalar(
                select(Tenant).where(Tenant.id == self.tenant).with_for_update()
            )
            if tenant is None:
                return {"cleaned": True, "already_absent": True}
            if tenant.purpose != "verification":
                raise ValueError("Non-verification tenant cleanup refused")
            for model in (Order, Reservation, Stock):
                s.execute(delete(model).where(model.tenant_id == self.tenant))
            s.delete(tenant)
        return {"cleaned": True, "tenant": self.tenant}

    def close(self):
        self.engine.dispose()
