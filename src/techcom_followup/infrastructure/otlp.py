"""Bounded OTLP/HTTP JSON log export without request-path network I/O."""

import asyncio
import json
import time
from contextlib import suppress
from queue import Empty, Full, Queue
from threading import Lock

import httpx
from prometheus_client import CollectorRegistry, Counter
from structlog.typing import EventDict, WrappedLogger

from techcom_followup.infrastructure.log_config import redact_event

MAX_QUEUE_CAPACITY = 4096
MAX_BATCH_RECORDS = 64
MAX_ACK_BYTES = 4096


class OtlpLogExporter:
    """Export already-redacted events; overload drops with a metric.

    OTLP/HTTP accepts JSON representations of ExportLogsServiceRequest.
    The exporter has no retry queue beyond its bounded input queue, uses
    one request per batch, and never logs collector responses or failures.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        registry: CollectorRegistry,
        *,
        capacity: int = 512,
    ) -> None:
        """Bound queued records and attach counters to the private registry."""
        if not 1 <= capacity <= MAX_QUEUE_CAPACITY:
            message = "OTLP queue capacity must be in 1..4096"
            raise ValueError(message)
        self._client, self._endpoint = client, endpoint
        self._queue: Queue[dict[str, object]] = Queue(capacity)
        self._admission = Lock()
        self._sending = False
        self._dropped = Counter(
            "followup_otlp_logs_dropped_total",
            "Dropped or failed log records",
            registry=registry,
        )
        self._exported = Counter(
            "followup_otlp_logs_exported_total",
            "Collector-accepted log records",
            registry=registry,
        )
        self._accepting = True

    def capture(
        self, _logger: WrappedLogger, _method: str, event: EventDict
    ) -> EventDict:
        """Run after the closed redaction processor, before JSON rendering."""
        event = redact_event(_logger, _method, event)
        record: dict[str, object] = {
            "timeUnixNano": str(time.time_ns()),
            "observedTimeUnixNano": str(time.time_ns()),
            "severityText": str(event["level"]).upper(),
            "severityNumber": {
                "debug": 5,
                "info": 9,
                "warning": 13,
                "error": 17,
                "critical": 21,
            }[str(event["level"])],
            "body": {"stringValue": json.dumps(event, ensure_ascii=True)},
            "attributes": [
                {"key": key, "value": {"stringValue": str(value)}}
                for key, value in event.items()
                if key not in {"event", "level"}
            ],
        }
        with self._admission:
            if not self._accepting:
                self._dropped.inc()
                return event
            try:
                self._queue.put_nowait(record)
            except Full:
                self._dropped.inc()
        return event

    async def run(self) -> None:
        """Own a single cancellable worker; request deadlines bound shutdown."""
        while True:
            try:
                first = self._queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.1)
                continue
            batch = [first]
            while len(batch) < MAX_BATCH_RECORDS:
                try:
                    batch.append(self._queue.get_nowait())
                except Empty:
                    break
            self._sending = True
            try:
                await self._send(batch)
            except asyncio.CancelledError:
                self._dropped.inc(len(batch))
                raise
            finally:
                self._sending = False

    async def _send(self, batch: list[dict[str, object]]) -> None:
        accepted = False
        with suppress(
            httpx.HTTPError, TimeoutError, ValueError, RecursionError
        ):
            async with asyncio.timeout(3):
                async with self._client.stream(
                    "POST",
                    self._endpoint,
                    headers={"Accept-Encoding": "identity"},
                    json={
                        "resourceLogs": [
                            {
                                "resource": {
                                    "attributes": [
                                        {
                                            "key": "service.name",
                                            "value": {
                                                "stringValue": "techcom-followup"
                                            },
                                        }
                                    ]
                                },
                                "scopeLogs": [
                                    {
                                        "scope": {"name": "techcom.safe-logs"},
                                        "logRecords": batch,
                                    }
                                ],
                            }
                        ]
                    },
                ) as response:
                    response.raise_for_status()
                    accepted = await self._acknowledged(response)
        (self._exported if accepted else self._dropped).inc(len(batch))

    @staticmethod
    async def _response_body(response: httpx.Response) -> bytes | None:
        """Bound wire bytes and avoid unrequested response decompression."""
        if response.is_stream_consumed:
            return (
                response.content
                if len(response.content) <= MAX_ACK_BYTES
                else None
            )
        raw = bytearray()
        async for chunk in response.aiter_raw(chunk_size=1024):
            raw.extend(chunk)
            if len(raw) > MAX_ACK_BYTES:
                return None
        return bytes(raw)

    @staticmethod
    async def _acknowledged(response: httpx.Response) -> bool:
        raw = await OtlpLogExporter._response_body(response)
        if raw is None:
            return False
        payload: object = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            return False
        partial = payload.get("partialSuccess", {})
        if not isinstance(partial, dict):
            return False
        rejected = partial.get("rejectedLogRecords", 0)
        return rejected == "0" or (type(rejected) is int and rejected == 0)

    async def close(self) -> None:
        """Stop admission and bound the drain; never retain logs on disk."""
        with self._admission:
            self._accepting = False
        with suppress(TimeoutError):
            async with asyncio.timeout(4):
                while not self._queue.empty() or self._sending:
                    await asyncio.sleep(0.05)
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break
            self._dropped.inc()
