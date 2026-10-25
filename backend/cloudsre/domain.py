from enum import StrEnum

from pydantic import BaseModel, Field


class Status(StrEnum):
    NEW = "NEW"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    ESCALATED = "ESCALATED"
    PLAN_READY = "PLAN_READY"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    RETRY_WAIT = "RETRY_WAIT"
    COMPENSATING = "COMPENSATING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


ALLOWED = {
    Status.NEW: {Status.INVESTIGATING},
    Status.INVESTIGATING: {
        Status.DIAGNOSED,
        Status.PLAN_READY,
        Status.ESCALATED,
        Status.RETRY_WAIT,
    },
    Status.DIAGNOSED: set(),
    Status.ESCALATED: set(),
    Status.PLAN_READY: {Status.WAITING_APPROVAL, Status.EXECUTING, Status.ESCALATED},
    Status.WAITING_APPROVAL: {Status.EXECUTING, Status.REJECTED, Status.ESCALATED},
    Status.CANCELLED: set(),
    Status.REJECTED: set(),
    Status.EXECUTING: {Status.VERIFYING, Status.ESCALATED},
    Status.VERIFYING: {Status.RESOLVED, Status.ESCALATED, Status.COMPENSATING},
    Status.RETRY_WAIT: {Status.INVESTIGATING, Status.ESCALATED},
    Status.COMPENSATING: {Status.ESCALATED, Status.VERIFYING},
    Status.RESOLVED: set(),
}


for _state in (
    Status.NEW,
    Status.INVESTIGATING,
    Status.RETRY_WAIT,
    Status.PLAN_READY,
    Status.WAITING_APPROVAL,
    Status.EXECUTING,
    Status.VERIFYING,
    Status.COMPENSATING,
):
    ALLOWED[_state].add(Status.CANCELLED)


class IncidentInput(BaseModel):
    dedup_key: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    service: str = Field(min_length=1, max_length=100)


def check_transition(old: Status, new: Status):
    if new not in ALLOWED[old]:
        raise ValueError(f"Invalid transition: {old} -> {new}")
