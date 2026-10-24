"""PostgreSQL session leases, fenced atomic checkpoints and immutable intents.

Session locks are authoritative (not a wall-clock TTL). A disconnected process
cannot renew or commit through a transparently reconnected database session.
Kubernetes resourceVersion + immutable action ID fence external side effects.
"""

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import MetaData, Table, insert, select, text, update

from .domain import Status, check_transition
from .store import Incident, WorkflowRun


def now():
    return datetime.now(UTC).isoformat()


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def lock_key(value):
    return int.from_bytes(
        hashlib.sha256(value.encode()).digest()[:8], "big", signed=True
    )


class LeaseLost(RuntimeError):
    pass


class Lease:
    def __init__(self, connection, incident, resource, owner, epoch, pid, tables):
        self.connection = connection
        self.incident = incident
        self.resource = resource
        self.owner = owner
        self.epoch = epoch
        self.pid = pid
        self.tables = tables
        self.closed = False

    @contextmanager
    def transaction(self):
        if self.closed or self.connection.closed or self.connection.invalidated:
            raise LeaseLost("Database session lease lost")
        with self.connection.begin():
            yield

    def assert_session(self):
        if self.closed or self.connection.closed or self.connection.invalidated:
            raise LeaseLost("Database session lease lost")
        pid = self.connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        if pid != self.pid:
            raise LeaseLost("Database session changed; stale worker fenced")

    def assert_owner(self):
        self.assert_session()
        t = self.tables["task_ownership"]
        row = (
            self.connection.execute(
                select(t).where(t.c.incident_id == self.incident).with_for_update()
            )
            .mappings()
            .one()
        )
        if (row["owner"], row["epoch"], row["resource_key"]) != (
            self.owner,
            self.epoch,
            self.resource,
        ):
            raise LeaseLost("Incident owner or fencing epoch changed")

    def check(self):
        with self.transaction():
            self.assert_owner()
            t = self.tables["task_ownership"]
            self.connection.execute(
                update(t)
                .where(t.c.incident_id == self.incident)
                .values(heartbeat_at=now())
            )

    def checkpoint(
        self, run_id, expected, target, payload, *, intent=None, result=None
    ):
        """All state, audit, journal and action changes commit in one transaction."""
        if (
            payload.get("incident_id") != self.incident
            or payload.get("run_id") != run_id
        ):
            raise ValueError("Checkpoint identity mismatch")
        if expected != target:
            check_transition(Status(expected), Status(target))
        serialized = encode(payload)
        with self.transaction():
            self.assert_owner()
            changed = self.connection.execute(
                update(Incident)
                .where(Incident.id == self.incident, Incident.status == str(expected))
                .values(status=str(target), diagnosis=payload.get("answer"))
            )
            if changed.rowcount != 1:
                raise LeaseLost("Incident state changed")
            old = self.connection.execute(
                select(WorkflowRun.id, WorkflowRun.incident_id).where(
                    WorkflowRun.id == run_id
                )
            ).first()
            if old and old.incident_id != self.incident:
                raise ValueError("Run identity collision")
            values = {
                "incident_id": self.incident,
                "phase": str(target),
                "updated_at": now(),
                "payload": serialized,
            }
            if old:
                self.connection.execute(
                    update(WorkflowRun).where(WorkflowRun.id == run_id).values(**values)
                )
            else:
                self.connection.execute(insert(WorkflowRun).values(id=run_id, **values))
            action_table = self.tables["action_intents"]
            if intent is not None:
                action_id = intent["id"]
                old_action = (
                    self.connection.execute(
                        select(action_table).where(action_table.c.id == action_id)
                    )
                    .mappings()
                    .first()
                )
                if old_action:
                    if (
                        old_action["spec_hash"] != digest(intent)
                        or old_action["incident_id"] != self.incident
                        or old_action["run_id"] != run_id
                    ):
                        raise ValueError("Immutable action intent changed")
                else:
                    self.connection.execute(
                        insert(action_table).values(
                            id=action_id,
                            incident_id=self.incident,
                            run_id=run_id,
                            state="PREPARED",
                            spec_hash=digest(intent),
                            spec=encode(intent),
                            created_at=now(),
                            updated_at=now(),
                        )
                    )
            if result is not None:
                if result.get("state") not in {
                    "DISPATCHED",
                    "APPLIED",
                    "CONFLICT",
                    "REJECTED",
                    "COMPENSATED",
                }:
                    raise ValueError("Unknown action result state")
                if result["state"] == "DISPATCHED" and target not in {
                    "EXECUTING",
                    "COMPENSATING",
                }:
                    raise ValueError("Dispatch outside executable phase")
                action = (
                    self.connection.execute(
                        select(action_table).where(action_table.c.id == result["id"])
                    )
                    .mappings()
                    .one()
                )
                if action["incident_id"] != self.incident or action["run_id"] != run_id:
                    raise ValueError("Action result belongs to another run")
                terminal = {"APPLIED", "CONFLICT", "REJECTED", "COMPENSATED"}
                if action["state"] in terminal and action["result"] != encode(result):
                    raise ValueError("Terminal action result is immutable")
                self.connection.execute(
                    update(action_table)
                    .where(action_table.c.id == result["id"])
                    .values(
                        state=result["state"], result=encode(result), updated_at=now()
                    )
                )
            audit = self.tables["workflow_audit"]
            self.connection.execute(
                insert(audit).values(
                    id=uuid.uuid4().hex,
                    incident_id=self.incident,
                    run_id=run_id,
                    owner=self.owner,
                    epoch=self.epoch,
                    phase=str(target),
                    at=now(),
                    payload_hash=hashlib.sha256(serialized.encode()).hexdigest(),
                    payload=serialized,
                )
            )
            t = self.tables["task_ownership"]
            self.connection.execute(
                update(t)
                .where(t.c.incident_id == self.incident)
                .values(heartbeat_at=now())
            )

    def action(self, action_id):
        with self.transaction():
            self.assert_owner()
            t = self.tables["action_intents"]
            row = (
                self.connection.execute(select(t).where(t.c.id == action_id))
                .mappings()
                .one()
            )
            if row["incident_id"] != self.incident:
                raise ValueError("Action ownership mismatch")
            return {
                **row,
                "spec": json.loads(row["spec"]),
                "result": json.loads(row["result"]) if row["result"] else None,
            }


class DurableStore:
    def __init__(self, store):
        self.store = store
        if store.engine.dialect.name != "postgresql":
            raise ValueError("Production leases require PostgreSQL")
        metadata = MetaData()
        self.tables = {
            n: Table(n, metadata, autoload_with=store.engine)
            for n in ("task_ownership", "workflow_audit", "action_intents")
        }

    @contextmanager
    def acquire(self, incident_id, resource_key):
        owner = uuid.uuid4().hex
        connection = self.store.engine.connect()
        keys = sorted(
            {lock_key("incident:" + incident_id), lock_key("resource:" + resource_key)}
        )
        acquired = []
        lease = None
        try:
            with connection.begin():
                pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
                for key in keys:
                    if not connection.execute(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                    ).scalar_one():
                        break
                    acquired.append(key)
                if len(acquired) == len(keys):
                    t = self.tables["task_ownership"]
                    old = (
                        connection.execute(
                            select(t)
                            .where(t.c.incident_id == incident_id)
                            .with_for_update()
                        )
                        .mappings()
                        .first()
                    )
                    epoch = old["epoch"] + 1 if old else 1
                    if old and old["resource_key"] != resource_key:
                        raise ValueError("Incident resource binding is immutable")
                    values = {
                        "owner": owner,
                        "epoch": epoch,
                        "resource_key": resource_key,
                        "heartbeat_at": now(),
                    }
                    if old:
                        connection.execute(
                            update(t)
                            .where(t.c.incident_id == incident_id)
                            .values(**values)
                        )
                    else:
                        connection.execute(
                            insert(t).values(incident_id=incident_id, **values)
                        )
                    lease = Lease(
                        connection,
                        incident_id,
                        resource_key,
                        owner,
                        epoch,
                        pid,
                        self.tables,
                    )
            yield lease
        finally:
            if lease:
                lease.closed = True
            # Invalidate rather than pool a connection whose session lock release is uncertain.
            try:
                if not connection.invalidated:
                    if connection.in_transaction():
                        connection.rollback()
                    with connection.begin():
                        for key in reversed(acquired):
                            connection.execute(
                                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                            )
            finally:
                connection.invalidate()
                connection.close()
