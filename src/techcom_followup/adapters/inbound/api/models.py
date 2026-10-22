"""Explicit HTTP models omit lease tokens and other worker-only data."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from techcom_followup.application.repository import ActionStatus
from techcom_followup.domain.models import DecisionKind, TargetKind


class ApiModel(BaseModel):
    """Reject undeclared input and avoid retaining caller mutations."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )


class TriggerRequest(ApiModel):
    """Only evaluation date and dry-run mode are caller-controlled."""

    as_of: date
    dry_run: bool = Field(default=False, strict=True)

    @field_validator("as_of", mode="before")
    @classmethod
    def canonical_date(cls, value: object) -> date:
        """Accept calendar dates and canonical JSON date strings."""
        if type(value) is date:
            return value
        if not isinstance(value, str):
            message = "evaluation date must be an ISO calendar date"
            msg = "calendar_date_type"
            raise PydanticCustomError(msg, message)
        parsed = date.fromisoformat(value)
        if value != parsed.isoformat():
            message = "evaluation date must use YYYY-MM-DD"
            raise ValueError(message)
        return parsed


class TriggerView(ApiModel):
    """Counts for global processing without notes or message bodies."""

    run_id: UUID | None
    status: Literal["completed", "dry_run", "skipped_locked"]
    enqueued: int = Field(ge=0)
    duplicates: int = Field(ge=0)


class RunCounts(ApiModel):
    """Safe aggregate fields extracted from trusted stored run stats."""

    decisions: int = Field(default=0, ge=0, strict=True)
    rejected: int = Field(default=0, ge=0, strict=True)
    enqueued: int = Field(default=0, ge=0, strict=True)
    duplicates: int = Field(default=0, ge=0, strict=True)


class RunView(ApiModel):
    """A run summary contains no recipients, task notes or messages."""

    run_id: UUID
    as_of: date
    policy_version: str
    started_at: datetime
    finished_at: datetime | None
    status: Literal["running", "completed", "failed"]
    counts: RunCounts


class ActionQuery(ApiModel):
    """Filters narrow authorized results; they never grant access."""

    task_id: (
        Annotated[str, Field(pattern=r"^[A-Z]{2,5}-[0-9]{1,9}$")] | None
    ) = None
    status: ActionStatus | None = None
    project: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=100, ge=1, le=100)


class TargetView(ApiModel):
    """A stored recipient relationship used for authorization."""

    kind: TargetKind
    name: str
    project: str | None


class ActionView(ApiModel):
    """Message facts available to an authorized actions reader."""

    action_id: UUID
    operation_id: UUID
    task_id: str
    decision: DecisionKind
    status: ActionStatus
    target: TargetView
    subject: str
    body: str
    attempts: int
    next_attempt_at: datetime
