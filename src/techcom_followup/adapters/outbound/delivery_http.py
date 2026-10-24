"""Single-attempt writes and strict acknowledgement checks."""

from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from techcom_followup.application.processing_ports import (
    Clock,
    DeliveryKind,
    DeliveryResult,
)
from techcom_followup.application.repository import (
    ActionRecord,
    require_storage_text,
)
from techcom_followup.infrastructure.http_client import (
    HttpEndpoint,
    UpstreamError,
    retry_after,
)

_RETRYABLE_CODES = frozenset({409, 429, 500, 502, 503, 504})
# A failed or timed-out gateway may have committed the write upstream;
# the outcome is unknown and reconciliation looks it up before a resend.
_AMBIGUOUS_WRITE_CODES = frozenset({500, 502, 504})


class _Receipt(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", hide_input_in_errors=True
    )
    operation_id: UUID
    provider_ref: Annotated[
        str,
        Field(min_length=1, max_length=200),
        AfterValidator(require_storage_text),
    ]
    status: Literal["sent"]


class HttpDeliveryGateway:
    """Lost acknowledgements need lookup before another POST."""

    def __init__(self, endpoint: HttpEndpoint, clock: Clock) -> None:
        """Bind a fixed outbound origin and injected clock."""
        self._endpoint = endpoint
        self._clock = clock

    async def send(self, action: ActionRecord) -> DeliveryResult:
        """Send a persisted payload once with its operation id."""
        target = action.decision.target
        if target is None:
            message = "an outbound action needs a policy-owned target"
            raise ValueError(message)
        payload = {
            "task_id": action.decision.task_id,
            "target": asdict(target),
            "message": {
                "subject": action.message.subject,
                "body": action.message.body,
                "template_id": action.message.template_id,
            },
        }
        try:
            response = await self._endpoint.request(
                "POST",
                "/v1/messages",
                payload=payload,
                operation_id=str(action.identity.operation_id),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return DeliveryResult(
                DeliveryKind.RETRYABLE, error="connect_failed"
            )
        except (httpx.HTTPError, UpstreamError, TimeoutError):
            return DeliveryResult(DeliveryKind.UNKNOWN, error="send_unknown")
        if response.status_code in _AMBIGUOUS_WRITE_CODES:
            return DeliveryResult(
                DeliveryKind.UNKNOWN, error=f"http_{response.status_code}"
            )
        return self._classify(response, action.identity.operation_id)

    async def lookup(self, operation_id: UUID) -> DeliveryResult:
        """Allow absence only after an authoritative lookup 404."""
        try:
            response = await self._endpoint.request(
                "GET", f"/v1/messages/{operation_id}"
            )
        except (httpx.HTTPError, UpstreamError, TimeoutError):
            return DeliveryResult(DeliveryKind.UNKNOWN, error="lookup_unknown")
        if response.status_code == httpx.codes.NOT_FOUND:
            return DeliveryResult(DeliveryKind.ABSENT)
        return self._classify(response, operation_id)

    def _classify(
        self, response: httpx.Response, operation_id: UUID
    ) -> DeliveryResult:
        code = response.status_code
        if code in _RETRYABLE_CODES:
            return DeliveryResult(
                DeliveryKind.RETRYABLE,
                error=f"http_{code}",
                retry_after=retry_after(
                    response.headers.get("Retry-After"), self._clock.now()
                ),
            )
        if response.is_success:
            return self._receipt(response, operation_id)
        return DeliveryResult(DeliveryKind.PERMANENT, error=f"http_{code}")

    @staticmethod
    def _receipt(
        response: httpx.Response, operation_id: UUID
    ) -> DeliveryResult:
        try:
            receipt = _Receipt.model_validate_json(response.content)
        except ValidationError:
            return DeliveryResult(
                DeliveryKind.UNKNOWN, error="invalid_receipt"
            )
        if receipt.operation_id != operation_id:
            return DeliveryResult(
                DeliveryKind.UNKNOWN, error="receipt_identity"
            )
        return DeliveryResult(DeliveryKind.ACCEPTED, receipt.provider_ref)
