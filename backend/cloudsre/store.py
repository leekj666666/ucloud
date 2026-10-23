import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import String, Text, create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .domain import IncidentInput, Status, check_transition
from .migrate import upgrade


class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    service: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40))
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertReceipt(Base):
    __tablename__ = "alert_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(36))
    alert_status: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(36))
    phase: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)


def as_dict(row):
    return {c.name: getattr(row, c.name) for c in Incident.__table__.columns}


class Store:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine)

    def initialize(self):
        upgrade(self.engine)

    def save_run(self, ident, incident_id, phase, payload):
        with self.sessions.begin() as s:
            row = s.get(WorkflowRun, ident)
            if row is None:
                row = WorkflowRun(id=ident, incident_id=incident_id)
                s.add(row)
            row.phase = phase
            row.updated_at = datetime.now(UTC).isoformat()
            row.payload = json.dumps(payload, ensure_ascii=False)

    def runs(self, ident):
        with self.sessions() as s:
            return [
                {
                    "id": r.id,
                    "phase": r.phase,
                    "updated_at": r.updated_at,
                    "payload": json.loads(r.payload),
                }
                for r in s.scalars(
                    select(WorkflowRun)
                    .where(WorkflowRun.incident_id == ident)
                    .order_by(WorkflowRun.updated_at)
                )
            ]

    def list(self):
        with self.sessions() as s:
            return [
                as_dict(r)
                for r in s.scalars(
                    select(Incident).order_by(Incident.created_at.desc()).limit(100)
                )
            ]

    def record_alert(self, ident, key, status, payload):
        receipt_id = hashlib.sha256(f"{key}:{status}".encode()).hexdigest()
        with self.sessions() as s:
            s.add(
                AlertReceipt(
                    id=receipt_id,
                    incident_id=ident,
                    alert_status=status,
                    received_at=datetime.now(UTC).isoformat(),
                    payload=json.dumps(payload),
                )
            )
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                if s.get(AlertReceipt, receipt_id) is None:
                    raise

    def receipts(self, ident):
        with self.sessions() as s:
            return [
                {"id": r.id, "status": r.alert_status, "payload": json.loads(r.payload)}
                for r in s.scalars(
                    select(AlertReceipt).where(AlertReceipt.incident_id == ident)
                )
            ]

    def create(self, data: IncidentInput):
        with self.sessions() as s:
            row = Incident(
                id=str(uuid.uuid4()),
                **data.model_dump(),
                status=Status.NEW,
                created_at=datetime.now(UTC).isoformat(),
            )
            s.add(row)
            try:
                s.commit()
                return as_dict(row), True
            except IntegrityError:
                s.rollback()
                existing = s.scalar(
                    select(Incident).where(Incident.dedup_key == data.dedup_key)
                )
                if existing is None:
                    raise
                if (existing.title, existing.service) != (data.title, data.service):
                    raise ValueError("Dedup key reused with different incident data")
                return as_dict(existing), False

    def get(self, ident):
        with self.sessions() as s:
            row = s.get(Incident, ident)
            return as_dict(row) if row else None

    def transition(self, ident, expected: Status, target: Status, diagnosis=None):
        check_transition(expected, target)
        with self.sessions.begin() as s:
            result = s.execute(
                update(Incident)
                .where(Incident.id == ident, Incident.status == expected)
                .values(status=target, diagnosis=diagnosis)
            )
            if result.rowcount != 1:
                raise ValueError(
                    "Incident missing or already claimed by another worker"
                )
