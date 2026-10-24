import gzip
import json
from uuid import uuid4

import httpx
import pytest

from persistence_support import NOW
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.delivery_http import (
    HttpDeliveryGateway,
)
from techcom_followup.application.processing_ports import DeliveryKind
from techcom_followup.infrastructure.http_client import (
    MAX_RESPONSE_BYTES,
    HttpEndpoint,
    UpstreamError,
    json_body,
)


@pytest.mark.parametrize("compressed", [False, True])
async def test_decoded_response_has_matching_representation_headers(
    compressed: bool,
) -> None:
    body = b'[{"task_id":"TC-1"}]'
    wire = gzip.compress(body) if compressed else body

    def respond(request: httpx.Request) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(wire)),
            "Retry-After": "12",
        }
        if compressed:
            headers["Content-Encoding"] = "gzip"
        return httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(wire)
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        endpoint = HttpEndpoint(
            client, "https://mock-api", allowed_hosts=frozenset({"mock-api"})
        )
        result = await endpoint.request("GET", "/v1/tasks")
    assert json_body(result) == [{"task_id": "TC-1"}]
    assert "content-encoding" not in result.headers
    assert "transfer-encoding" not in result.headers
    assert result.headers["content-length"] == str(len(body))
    assert result.headers["retry-after"] == "12"


async def test_compressed_response_still_obeys_decoded_size_limit() -> None:
    wire = gzip.compress(b"x" * (MAX_RESPONSE_BYTES + 1))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(wire),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        endpoint = HttpEndpoint(
            client, "https://mock-api", allowed_hosts=frozenset({"mock-api"})
        )
        with pytest.raises(UpstreamError, match="response_size"):
            await endpoint.request("GET", "/v1/tasks")


@pytest.mark.parametrize("reference", ["valid-réf", "invalid\x00reference"])
@pytest.mark.parametrize("compressed", [False, True])
async def test_receipts_are_validated_for_persistent_storage(
    reference: str,
    compressed: bool,
) -> None:
    operation = uuid4()

    def respond(request: httpx.Request) -> httpx.Response:
        body = {
            "operation_id": str(operation),
            "status": "sent",
            "provider_ref": reference,
        }
        content = json.dumps(body).encode()
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"} if compressed else {},
            stream=httpx.ByteStream(
                gzip.compress(content) if compressed else content
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        endpoint = HttpEndpoint(
            client, "https://mock-api", allowed_hosts=frozenset({"mock-api"})
        )
        result = await HttpDeliveryGateway(endpoint, FixedClock(NOW)).lookup(
            operation
        )
    if "\x00" in reference:
        assert result.kind is DeliveryKind.UNKNOWN
        assert result.error == "invalid_receipt"
    else:
        assert result.kind is DeliveryKind.ACCEPTED
        assert result.provider_ref == reference
