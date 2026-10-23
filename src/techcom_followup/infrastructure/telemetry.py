"""Prometheus implementation of the telemetry port (``docs/14`` s. 8).

One registry per process. The API exposes it on its private metrics
listener; CLI commands can write it to a node_exporter textfile. Outbox
gauges are refreshed from the database right before each export, so
they describe shared state no matter which process changed it.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from functools import partial
from pathlib import Path

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    write_to_textfile,
)

from techcom_followup.application.repository import ActionStatus, OutboxStatus
from techcom_followup.application.telemetry import CounterName, HistogramName

_COUNTERS: dict[CounterName, tuple[str, tuple[str, ...]]] = {
    CounterName.RUNS: ("Processing runs by result", ("result",)),
    CounterName.TASKS: ("Source records by outcome", ("outcome",)),
    CounterName.DECISIONS: (
        "Decisions by kind and rule",
        ("decision", "rule"),
    ),
    CounterName.ACTIONS: ("Outbox actions by outcome", ("outcome",)),
    CounterName.DELIVERY_ATTEMPTS: (
        "Delivery attempts by result",
        ("result",),
    ),
    CounterName.LLM_REQUESTS: (
        "Language model calls by purpose and result",
        ("purpose", "result"),
    ),
    CounterName.LLM_TOKENS: (
        "Language model tokens by purpose and direction",
        ("purpose", "direction"),
    ),
    CounterName.LLM_COST: (
        "Estimated language model spend in USD",
        ("purpose",),
    ),
    CounterName.AGENT_PLANS: ("Orchestrator plans by outcome", ("outcome",)),
    CounterName.SECURITY_EVENTS: ("Security events by kind", ("kind",)),
}
_HISTOGRAMS: dict[
    HistogramName, tuple[str, tuple[str, ...], tuple[float, ...]]
] = {
    HistogramName.RUN_DURATION: (
        "Processing run duration in seconds",
        (),
        (1, 5, 15, 30, 60, 120, 300, 600),
    ),
    HistogramName.LLM_LATENCY: (
        "Language model call latency in seconds",
        ("purpose",),
        (0.25, 0.5, 1, 2, 5, 10, 20, 40),
    ),
}
CIRCUIT_STATES = ("closed", "open", "half_open")
_TERMINAL = frozenset({"sent", "dead"})


class PrometheusTelemetry:
    """Bounded metric families in a private registry."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        """Create every catalogued family so exports are complete."""
        self.registry = (
            registry if registry is not None else (CollectorRegistry())
        )
        self._counters = {
            name: Counter(name, text, labels, registry=self.registry)
            for name, (text, labels) in _COUNTERS.items()
        }
        self._histograms = {
            name: Histogram(
                name, text, labels, buckets=buckets, registry=self.registry
            )
            for name, (text, labels, buckets) in _HISTOGRAMS.items()
        }
        self._backlog = Gauge(
            "followup_outbox_backlog",
            "Outbox actions per status",
            ("status",),
            registry=self.registry,
        )
        self._oldest = Gauge(
            "followup_outbox_oldest_age_seconds",
            "Seconds the earliest due action of a status has waited",
            ("status",),
            registry=self.registry,
        )
        self._circuit = Gauge(
            "followup_circuit_state",
            "1 for the current state of a dependency's circuit breaker",
            ("dependency", "state"),
            registry=self.registry,
        )

    def count(
        self, name: CounterName, amount: float = 1, **labels: str
    ) -> None:
        """Add to a counter; label names are fixed per family."""
        family = self._counters[name]
        (family.labels(**labels) if labels else family).inc(amount)

    def observe(
        self, name: HistogramName, value: float, **labels: str
    ) -> None:
        """Record a duration; label names are fixed per family."""
        family = self._histograms[name]
        (family.labels(**labels) if labels else family).observe(value)

    def watch_circuit(self, dependency: str, state: Callable[[], str]) -> None:
        """Read a breaker's state lazily at every export."""
        for name in CIRCUIT_STATES:
            self._circuit.labels(dependency, name).set_function(
                partial(_is_state, state, name)
            )

    def set_outbox(
        self, statuses: Iterable[OutboxStatus], now: datetime
    ) -> None:
        """Replace outbox gauges with a fresh database summary."""
        seen = {item.status: item for item in statuses}
        for status in ActionStatus:
            item = seen.get(status)
            count = 0 if item is None else item.count
            self._backlog.labels(status.value).set(count)
            due = None if item is None else item.earliest_due
            waited = (
                0.0
                if due is None or status.value in _TERMINAL
                else (now - due).total_seconds()
            )
            self._oldest.labels(status.value).set(max(0.0, waited))

    def write_textfile(self, path: Path) -> None:
        """Write the registry atomically for node_exporter."""
        write_to_textfile(str(path), self.registry)


def _is_state(state: Callable[[], str], name: str) -> float:
    return float(state() == name)
