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


ALLOWED = {
    Status.NEW: {Status.INVESTIGATING},
    Status.INVESTIGATING: {Status.DIAGNOSED, Status.PLAN_READY, Status.ESCALATED},
    Status.DIAGNOSED: set(),
    Status.ESCALATED: set(),
    Status.PLAN_READY: {Status.EXECUTING, Status.ESCALATED},
    Status.EXECUTING: {Status.VERIFYING, Status.ESCALATED},
    Status.VERIFYING: {Status.RESOLVED, Status.ESCALATED},
    Status.RESOLVED: set(),
}


class IncidentInput(BaseModel):
    dedup_key: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    service: str = Field(min_length=1, max_length=100)


def check_transition(old: Status, new: Status):
    if new not in ALLOWED[old]:
        raise ValueError(f"Invalid transition: {old} -> {new}")
