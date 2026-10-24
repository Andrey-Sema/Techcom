"""Bounded transport models, separate from production domain objects."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Reject coercions, extra fields and mutable transport state."""

    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True
    )


class Recipient(StrictModel):
    """Recipient supplied by deterministic application policy."""

    kind: Literal["person", "role", "team"]
    name: str = Field(min_length=1, max_length=160)
    project: str | None = Field(default=None, max_length=160)


class Message(StrictModel):
    """A rendered message; the receiver never evaluates templates."""

    subject: str = Field(min_length=1, max_length=120, pattern=r"^[^\r\n]*$")
    body: str = Field(min_length=20, max_length=800)
    template_id: str = Field(min_length=1, max_length=100)


class DeliveryPayload(StrictModel):
    """Message delivery contract without task mutation capabilities."""

    task_id: str = Field(min_length=1, max_length=100)
    target: Recipient
    message: Message


class Receipt(StrictModel):
    """Stable acknowledgement returned on creation and replay."""

    operation_id: UUID
    provider_ref: str
    status: Literal["sent"] = "sent"


class ErrorBody(StrictModel):
    """Error envelope: a fixed message, never the request input."""

    detail: str


class Absence(StrictModel):
    """A lookup 404 naming the operation; a routing 404 cannot."""

    operation_id: UUID
    status: Literal["not_found"]


class DeliveryEntry(StrictModel):
    """One committed JSONL record, including its payload digest."""

    payload_sha256: str
    payload: DeliveryPayload
    receipt: Receipt
