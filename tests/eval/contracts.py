"""Closed evaluation formats with explicit replay provenance."""

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Self

from openai.types.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from techcom_followup.application.ports import LLMPurpose, LLMRequest
from techcom_followup.domain.models import (
    BlockerCategory,
    DecisionKind,
    TaskStatus,
)


class Contract(BaseModel):
    """Reject coercion and undeclared evaluation metadata."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class EvalCase(Contract):
    """Independent semantic labels and expected policy behavior."""

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,60}$")
    note: str = Field(max_length=4000)
    label: BlockerCategory
    expected_decision: DecisionKind
    injection: bool = False
    status: TaskStatus = TaskStatus.WORKING_ON_IT
    due_days: int | None = -1
    has_owner: bool = True
    challenge: str = Field(min_length=1, max_length=100)


class Source(StrEnum):
    """Distinguish synthetic scripts from provider captures."""

    SYNTHETIC = "synthetic"
    PROVIDER = "provider"


class ProviderEvidence(Contract):
    """Externally supplied capture metadata, not proof of remote origin.

    Offline replay can check consistency, never authenticate an HTTP
    origin. A curator must independently verify capture provenance.
    """

    endpoint: str = Field(pattern=r"^https://api\.openai\.com/v1/responses$")
    captured_at: str = Field(min_length=1)
    operator_attestation: str = Field(min_length=20)
    raw_response: dict[str, object]


class Recording(Contract):
    """A single response bound to the exact prompt, schema and input."""

    case_id: str
    purpose: LLMPurpose
    prompt_version: str
    schema_name: str
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    instructions_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: Source
    model: str
    response_id: str
    text: str = Field(max_length=32768)
    provider_evidence: ProviderEvidence | None = None

    @model_validator(mode="after")
    def provenance(self) -> Self:
        """Validate source identity and raw capture consistency."""
        if self.source == Source.SYNTHETIC:
            if (
                self.model != "synthetic.contract.v1"
                or self.provider_evidence is not None
            ):
                message = "synthetic records need the synthetic model identity"
                raise ValueError(message)
        else:
            evidence = self.provider_evidence
            if evidence is None:
                message = "provider records require raw capture evidence"
                raise ValueError(message)
            raw = Response.model_validate(evidence.raw_response)
            if (
                raw.id != self.response_id
                or raw.model != self.model
                or raw.output_text != self.text
            ):
                message = "provider capture does not match replay metadata"
                raise ValueError(message)
        return self

    def verify_request(self, request: LLMRequest) -> None:
        """Reject fixture drift before replay."""
        actual = {
            "purpose": request.purpose,
            "prompt_version": request.prompt_version,
            "schema_name": request.schema_name,
            "schema_sha256": digest_json(dict(request.json_schema)),
            "instructions_sha256": digest_text(request.instructions),
            "input_sha256": digest_text(request.input_text),
        }
        if any(
            getattr(self, field) != value for field, value in actual.items()
        ):
            message = "recording prompt, schema or input provenance mismatch"
            raise ValueError(message)


class Manifest(Contract):
    """Bind one corpus and its complete response bundle."""

    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recordings_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_version: str
    provenance: str = Field(min_length=20)
    interpretation_prompt: str = "interp.v1"
    drafting_prompt: str = "draft.v2"


def digest_text(text: str) -> str:
    """Hash Unicode without failing on hostile surrogate payloads."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def digest_json(value: object) -> str:
    """Hash canonical JSON."""
    return digest_text(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
    )


def load_bundle(
    dataset: Path, recordings: Path, manifest: Path
) -> tuple[tuple[EvalCase, ...], tuple[Recording, ...], Manifest]:
    """Validate bundle hashes, case IDs and membership."""
    data_text, recording_text = dataset.read_text(), recordings.read_text()
    metadata = Manifest.model_validate_json(manifest.read_text())
    if (
        digest_text(data_text) != metadata.dataset_sha256
        or digest_text(recording_text) != metadata.recordings_sha256
    ):
        message = "evaluation bundle content digest mismatch"
        raise ValueError(message)
    cases = tuple(
        EvalCase.model_validate_json(line) for line in data_text.splitlines()
    )
    responses = tuple(
        Recording.model_validate_json(line)
        for line in recording_text.splitlines()
    )
    ids = {case.case_id for case in cases}
    if (
        not cases
        or len(ids) != len(cases)
        or any(row.case_id not in ids for row in responses)
    ):
        message = "dataset case IDs must be unique and cover every recording"
        raise ValueError(message)
    return cases, responses, metadata
