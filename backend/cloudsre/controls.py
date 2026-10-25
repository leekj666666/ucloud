"""Operator authority is durable and bound to an exact immutable action."""

import json
import uuid
from contextlib import contextmanager

from sqlalchemy import String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .actions import NAMESPACES
from .durable import digest, now
from .store import AlertReceipt, Base, Incident, WorkflowRun, as_dict

TERMINAL = {"RESOLVED", "ESCALATED", "DIAGNOSED", "CANCELLED", "REJECTED"}
SERVICES = {"order", "inventory", "gateway"}


class ControlConflict(ValueError):
    pass


class OperatorStop(Exception):
    pass


class IncidentControl(Base):
    __tablename__ = "incident_controls"
    incident_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64))
    execution_mode: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[str] = mapped_column(String(100))
    requested_at: Mapped[str] = mapped_column(String(40))
    stop_requested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PlanDecision(Base):
    __tablename__ = "plan_decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(36))
    run_id: Mapped[str] = mapped_column(String(64))
    action_id: Mapped[str] = mapped_column(String(64), unique=True)
    intent_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class OperatorEvent(Base):
    __tablename__ = "operator_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(100))
    at: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)


def columns(row):
    return (
        {c.name: getattr(row, c.name) for c in row.__table__.columns} if row else None
    )


def event(session, ident, kind, actor, payload):
    session.add(
        OperatorEvent(
            id=uuid.uuid4().hex,
            incident_id=ident,
            kind=kind,
            actor=actor,
            at=now(),
            payload=json.dumps(payload, ensure_ascii=False),
        )
    )


class Controls:
    def __init__(self, store):
        self.store = store

    def pending(self):
        with self.store.sessions() as s:
            return [
                as_dict(row)
                for row in s.scalars(
                    select(Incident)
                    .join(IncidentControl, IncidentControl.incident_id == Incident.id)
                    .where(Incident.status.not_in(TERMINAL))
                    .order_by(IncidentControl.requested_at, Incident.id)
                    .limit(100)
                )
            ]

    def get(self, ident):
        with self.store.sessions() as s:
            return columns(s.get(IncidentControl, ident))

    def decision(self, action_id):
        with self.store.sessions() as s:
            return columns(
                s.scalar(
                    select(PlanDecision).where(PlanDecision.action_id == action_id)
                )
            )

    def events(self, ident):
        with self.store.sessions() as s:
            return [
                {**columns(row), "payload": json.loads(row.payload)}
                for row in s.scalars(
                    select(OperatorEvent)
                    .where(OperatorEvent.incident_id == ident)
                    .order_by(OperatorEvent.at, OperatorEvent.id)
                )
            ]

    def create(self, *, title, service, namespace, execution_mode, request_id, actor):
        if (
            service not in SERVICES
            or namespace not in NAMESPACES
            or execution_mode not in {"manual", "auto"}
        ):
            raise ValueError("Unsupported task target or execution mode")
        key = "console:" + request_id
        try:
            with self.store.sessions.begin() as s:
                row = Incident(
                    id=str(uuid.uuid4()),
                    dedup_key=key,
                    title=title,
                    service=service,
                    status="NEW",
                    created_at=now(),
                )
                s.add(row)
                s.flush()
                s.add(
                    IncidentControl(
                        incident_id=row.id,
                        namespace=namespace,
                        execution_mode=execution_mode,
                        requested_by=actor,
                        requested_at=now(),
                    )
                )
                event(
                    s,
                    row.id,
                    "task_started",
                    actor,
                    {"namespace": namespace, "execution_mode": execution_mode},
                )
                return as_dict(row), True
        except IntegrityError:
            with self.store.sessions() as s:
                old = s.scalar(select(Incident).where(Incident.dedup_key == key))
                control = s.get(IncidentControl, old.id) if old else None
                if (
                    not old
                    or not control
                    or (
                        old.title,
                        old.service,
                        control.namespace,
                        control.execution_mode,
                    )
                    != (title, service, namespace, execution_mode)
                ):
                    raise ControlConflict(
                        "Request ID reused with different task"
                    ) from None
                return as_dict(old), False

    def start(self, ident, namespace, mode, actor):
        if namespace not in NAMESPACES or mode not in {"manual", "auto"}:
            raise ValueError("Unsupported namespace or execution mode")
        with self.store.sessions.begin() as s:
            row = s.scalar(
                select(Incident).where(Incident.id == ident).with_for_update()
            )
            if not row:
                raise KeyError(ident)
            existing = s.get(IncidentControl, ident)
            if existing:
                if (existing.namespace, existing.execution_mode) != (namespace, mode):
                    raise ControlConflict(
                        "Existing task mode and namespace are immutable"
                    )
                return columns(existing)
            if row.status != "NEW" or s.scalar(
                select(WorkflowRun.id).where(WorkflowRun.incident_id == ident).limit(1)
            ):
                raise ControlConflict(
                    "Only an unstarted event can start a console task"
                )
            receipts = list(
                s.scalars(select(AlertReceipt).where(AlertReceipt.incident_id == ident))
            )
            observed = {
                json.loads(receipt.payload)
                .get("labels", {})
                .get("namespace", "sre-demo")
                for receipt in receipts
            }
            if receipts and observed != {namespace}:
                raise ControlConflict("Namespace must match the observed incident")
            control = IncidentControl(
                incident_id=ident,
                namespace=namespace,
                execution_mode=mode,
                requested_by=actor,
                requested_at=now(),
            )
            s.add(control)
            event(
                s,
                ident,
                "task_started",
                actor,
                {"namespace": namespace, "execution_mode": mode},
            )
            return columns(control)

    def decide(self, ident, *, run_id, action_id, intent_hash, decision, reason, actor):
        if decision not in {"approve", "reject"}:
            raise ValueError("Unknown decision")
        with self.store.sessions.begin() as s:
            control = s.scalar(
                select(IncidentControl)
                .where(IncidentControl.incident_id == ident)
                .with_for_update()
            )
            if not control or control.execution_mode != "manual":
                raise ControlConflict("This incident is not awaiting manual authority")
            if control.stop_requested_at:
                raise ControlConflict("A stopped task cannot be approved")
            old = s.scalar(
                select(PlanDecision).where(PlanDecision.action_id == action_id)
            )
            if old:
                if (old.incident_id, old.run_id, old.intent_hash, old.decision) != (
                    ident,
                    run_id,
                    intent_hash,
                    decision,
                ):
                    raise ControlConflict("An immutable decision already exists")
                return columns(old)
            row = s.get(Incident, ident)
            run = s.get(WorkflowRun, run_id)
            if (
                not row
                or not run
                or run.incident_id != ident
                or row.status != "WAITING_APPROVAL"
                or run.phase != "WAITING_APPROVAL"
            ):
                raise ControlConflict("Task is not waiting for this plan")
            intent = json.loads(run.payload).get("action_intent", {})
            if intent.get("id") != action_id or digest(intent) != intent_hash:
                raise ControlConflict(
                    "Plan identity or version changed; reload the plan"
                )
            record = PlanDecision(
                id=uuid.uuid4().hex,
                incident_id=ident,
                run_id=run_id,
                action_id=action_id,
                intent_hash=intent_hash,
                decision=decision,
                actor=actor,
                reason=reason,
                created_at=now(),
            )
            s.add(record)
            event(
                s,
                ident,
                "plan_" + decision,
                actor,
                {
                    "run_id": run_id,
                    "action_id": action_id,
                    "intent_hash": intent_hash,
                    "reason": reason,
                },
            )
            return columns(record)

    def stop(self, ident, reason, actor):
        # Same row lock as dispatch_guard: a successful response fences later writes.
        with self.store.sessions.begin() as s:
            control = s.scalar(
                select(IncidentControl)
                .where(IncidentControl.incident_id == ident)
                .with_for_update()
            )
            if not control:
                raise ControlConflict("This historical event has no console task")
            row = s.get(Incident, ident)
            if row.status in TERMINAL and not control.stop_requested_at:
                raise ControlConflict("Task already finished")
            if not control.stop_requested_at:
                control.stop_requested_at = now()
                control.stop_reason = reason
                event(s, ident, "stop_requested", actor, {"reason": reason})
            return {
                "requested_at": control.stop_requested_at,
                "status": row.status,
                "acknowledged": row.status in TERMINAL,
                "reason": control.stop_reason,
            }

    def check(self, ident):
        control = self.get(ident)
        if control and control["stop_requested_at"]:
            raise OperatorStop(control["stop_reason"] or "Operator requested stop")
        return control

    @contextmanager
    def dispatch_guard(self, ident, intent, run_id):
        with self.store.sessions.begin() as s:
            control = s.scalar(
                select(IncidentControl)
                .where(IncidentControl.incident_id == ident)
                .with_for_update()
            )
            if control and control.stop_requested_at:
                raise OperatorStop(control.stop_reason or "Operator requested stop")
            if control and control.execution_mode == "manual":
                decision = s.scalar(
                    select(PlanDecision).where(PlanDecision.action_id == intent["id"])
                )
                if not decision or (
                    decision.incident_id,
                    decision.run_id,
                    decision.intent_hash,
                    decision.decision,
                ) != (ident, run_id, digest(intent), "approve"):
                    raise ControlConflict(
                        "Exact operator approval required before write"
                    )
            yield

    @contextmanager
    def checkpoint_guard(self, ident):
        with self.store.sessions.begin() as s:
            control = s.scalar(
                select(IncidentControl)
                .where(IncidentControl.incident_id == ident)
                .with_for_update()
            )
            if control and control.stop_requested_at:
                raise OperatorStop(control.stop_reason or "Operator requested stop")
            yield
