"""Bounded metrics and isolated structured security events."""

from contextlib import suppress

import structlog
from prometheus_client import Counter

from techcom_followup.adapters.inbound.api.ports import (
    EventSink,
    SecurityEvent,
)
from techcom_followup.application.telemetry import CounterName
from techcom_followup.infrastructure.telemetry import PrometheusTelemetry


class StructuredEventSink:
    """Structured logging, replaceable by an OTLP sink."""

    def emit(self, event: SecurityEvent) -> None:
        """Count sink failures without breaking request processing."""
        structlog.get_logger("techcom_followup.security").info(
            event.name,
            request_id=str(event.request_id),
            status=event.status,
            actor_id=event.actor_id,
        )


class Observer:
    """Isolated metrics with protection against sink errors."""

    def __init__(
        self,
        sink: EventSink | None = None,
        *,
        telemetry: PrometheusTelemetry | None = None,
    ) -> None:
        """Build API counters next to the process telemetry families."""
        self.telemetry = (
            telemetry if telemetry is not None else PrometheusTelemetry()
        )
        self.registry = self.telemetry.registry
        self._requests = Counter(
            "followup_api_requests_total",
            "API response outcomes",
            ("status",),
            registry=self.registry,
        )
        self._logging_errors = Counter(
            "followup_api_logging_errors_total",
            "Failed event sink writes",
            registry=self.registry,
        )
        self._sink = sink if sink is not None else StructuredEventSink()

    def response(self, status: int) -> None:
        """Record an HTTP status with a bounded integer label."""
        self._requests.labels(str(status)).inc()

    def emit(self, event: SecurityEvent) -> None:
        """Count sink failures without breaking request processing."""
        self.telemetry.count(
            CounterName.SECURITY_EVENTS, kind=event.name.split(".", 1)[-1]
        )
        emitted = False
        with suppress(Exception):
            self._sink.emit(event)
            emitted = True
        if not emitted:
            self._logging_errors.inc()
