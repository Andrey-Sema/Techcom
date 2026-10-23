"""OTLP privacy, bounded overload and protocol serialization."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import override
from uuid import uuid4

import httpx
from hypothesis import given, settings
from hypothesis import strategies as st
from prometheus_client import CollectorRegistry

from techcom_followup.infrastructure.otlp import OtlpLogExporter


async def test_logs_reach_otlp_as_closed_json_without_secret_fields() -> None:
    sent = asyncio.Event()
    bodies: list[dict[str, object]] = []

    async def collector(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/logs"
        bodies.append(json.loads(request.content))
        sent.set()
        return httpx.Response(200, json={})

    registry = CollectorRegistry()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(collector)
    ) as client:
        exporter = OtlpLogExporter(
            client, "https://collector.test/v1/logs", registry
        )
        worker = asyncio.create_task(exporter.run())
        try:
            request_id = str(uuid4())
            exporter.capture(
                None,
                "info",
                {
                    "event": "security.authn_succeeded",
                    "request_id": request_id,
                    "status": 200,
                    "actor_id": "a" * 24,
                    "authorization": "Bearer PRIVATE",
                    "note": "RAW-NOTE",
                },
            )
            async with asyncio.timeout(5):
                await sent.wait()
            await exporter.close()
            serialized = json.dumps(bodies)
            assert "resourceLogs" in serialized
            assert request_id in serialized
            assert "security.authn_succeeded" in serialized
            assert "PRIVATE" not in serialized
            assert "RAW-NOTE" not in serialized
            assert (
                registry.get_sample_value("followup_otlp_logs_exported_total")
                == 1
            )
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


@settings(deadline=None, max_examples=30)
@given(secret=st.text(min_size=5, max_size=200))
async def test_overload_is_bounded_for_arbitrary_untrusted_fields(
    secret: str,
) -> None:
    registry = CollectorRegistry()
    marker = "PRIVATE:" + secret
    async with httpx.AsyncClient() as client:
        exporter = OtlpLogExporter(
            client, "https://collector.test/v1/logs", registry, capacity=1
        )
        for _ in range(2):
            exporter.capture(
                None,
                "info",
                {
                    "event": "command.completed",
                    "secret": marker,
                },
            )
        assert exporter._queue.qsize() == 1
        assert (
            registry.get_sample_value("followup_otlp_logs_dropped_total") == 1
        )
        assert marker not in str(exporter._queue.get_nowait())


async def test_partial_rejection_and_malformed_json_fail_closed() -> None:
    payloads: tuple[object, ...] = (
        {"partialSuccess": {"rejectedLogRecords": "1"}},
        [],
        None,
    )
    for payload in payloads:

        async def collector(
            _request: httpx.Request, captured: object = payload
        ) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(captured))

        registry = CollectorRegistry()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(collector)
        ) as client:
            exporter = OtlpLogExporter(
                client, "https://collector.test/v1/logs", registry
            )
            await exporter._send(
                [{"body": {"stringValue": "command.completed"}}]
            )
            assert (
                registry.get_sample_value("followup_otlp_logs_dropped_total")
                == 1
            )


async def test_streamed_collector_ack_is_bounded() -> None:
    class Chunks(httpx.AsyncByteStream):
        def __init__(self, data: bytes) -> None:
            self.data = data

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for start in range(0, len(self.data), 1024):
                yield self.data[start : start + 1024]

    for data, expected in ((b"{}", 1), (b" " * 4097, 0)):

        async def collector(
            _request: httpx.Request, captured: bytes = data
        ) -> httpx.Response:
            return httpx.Response(200, stream=Chunks(captured))

        registry = CollectorRegistry()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(collector)
        ) as client:
            exporter = OtlpLogExporter(
                client, "https://collector.test/v1/logs", registry
            )
            await exporter._send(
                [{"body": {"stringValue": "command.completed"}}]
            )
            assert (
                registry.get_sample_value("followup_otlp_logs_exported_total")
                == expected
            )
            assert (
                registry.get_sample_value("followup_otlp_logs_dropped_total")
                == 1 - expected
            )
