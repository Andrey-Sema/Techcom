"""OTLP admission, cancellation and bounded shutdown accounting."""

import asyncio
import json

import httpx
import pytest
from prometheus_client import CollectorRegistry

from techcom_followup.infrastructure.otlp import OtlpLogExporter

ENDPOINT = "https://collector.test/v1/logs"
EVENT = {"event": "command.completed", "level": "info"}


def dropped(registry: CollectorRegistry) -> float | None:
    return registry.get_sample_value("followup_otlp_logs_dropped_total")


@pytest.mark.parametrize("capacity", [0, 4097])
async def test_queue_capacity_is_bounded(capacity: int) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match=r"in 1\.\.4096"):
            OtlpLogExporter(
                client, ENDPOINT, CollectorRegistry(), capacity=capacity
            )


async def test_closed_exporter_drops_instead_of_queueing() -> None:
    registry = CollectorRegistry()
    async with httpx.AsyncClient() as client:
        exporter = OtlpLogExporter(client, ENDPOINT, registry)
        await exporter.close()
        exporter.capture(None, "info", dict(EVENT))
        assert exporter._queue.empty()
        assert dropped(registry) == 1


async def test_cancelled_send_counts_the_whole_batch_as_dropped() -> None:
    entered = asyncio.Event()

    async def collector(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    registry = CollectorRegistry()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(collector)
    ) as client:
        exporter = OtlpLogExporter(client, ENDPOINT, registry)
        worker = asyncio.create_task(exporter.run())
        # An idle worker polls; the first records arrive while it waits.
        await asyncio.sleep(0.15)
        for _ in range(3):
            exporter.capture(None, "info", dict(EVENT))
        async with asyncio.timeout(5):
            await entered.wait()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert dropped(registry) == 3
        assert not exporter._sending


async def test_rejected_partial_success_shape_fails_closed() -> None:
    async def collector(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"partialSuccess": []}))

    registry = CollectorRegistry()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(collector)
    ) as client:
        exporter = OtlpLogExporter(client, ENDPOINT, registry)
        await exporter._send([{"body": {"stringValue": "x"}}])
        assert dropped(registry) == 1


async def test_close_waits_for_the_drain_then_discards_leftovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CollectorRegistry()
    monkeypatch.setattr("techcom_followup.infrastructure.otlp.DRAIN_POLLS", 2)
    async with httpx.AsyncClient() as client:
        exporter = OtlpLogExporter(client, ENDPOINT, registry)
        for _ in range(2):
            exporter.capture(None, "info", dict(EVENT))
        # No worker drains the queue: close waits for its bound, then
        # discards and counts what is left instead of keeping it.
        await exporter.close()
        assert exporter._queue.empty()
        assert dropped(registry) == 2


async def test_worker_sends_at_most_one_full_batch_per_request() -> None:
    sizes: list[int] = []
    done = asyncio.Event()

    async def collector(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        (scope,) = body["resourceLogs"][0]["scopeLogs"]
        sizes.append(len(scope["logRecords"]))
        if sum(sizes) == 65:
            done.set()
        return httpx.Response(200, json={})

    registry = CollectorRegistry()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(collector)
    ) as client:
        exporter = OtlpLogExporter(client, ENDPOINT, registry)
        for _ in range(65):
            exporter.capture(None, "info", dict(EVENT))
        worker = asyncio.create_task(exporter.run())
        try:
            async with asyncio.timeout(5):
                await done.wait()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    assert sizes == [64, 1]


@pytest.mark.parametrize("status", [500, 503])
async def test_collector_errors_are_counted_not_raised(status: int) -> None:
    async def collector(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="PRIVATE collector detail")

    registry = CollectorRegistry()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(collector)
    ) as client:
        exporter = OtlpLogExporter(client, ENDPOINT, registry)
        await exporter._send([{"body": {"stringValue": "x"}}] * 2)
        assert dropped(registry) == 2


async def test_send_deadline_is_counted_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collector(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)

    real_timeout = asyncio.timeout
    monkeypatch.setattr(
        "techcom_followup.infrastructure.otlp.asyncio.timeout",
        lambda _delay: real_timeout(0.05),
    )
    registry = CollectorRegistry()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(collector)
    ) as client:
        exporter = OtlpLogExporter(client, ENDPOINT, registry)
        await exporter._send([{"body": {"stringValue": "x"}}])
        assert dropped(registry) == 1
