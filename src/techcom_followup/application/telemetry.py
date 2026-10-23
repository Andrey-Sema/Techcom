"""Telemetry port: closed metric names with bounded label values.

Use cases report what happened; an infrastructure adapter turns it into
Prometheus series. Every label value passed here comes from a closed
enum or a rule identifier, never from task data, so cardinality stays
bounded and no personal or note text reaches metrics.
"""

from enum import StrEnum
from typing import Protocol


class CounterName(StrEnum):
    """Counters of the catalogue in ``docs/14`` section 8."""

    RUNS = "followup_runs_total"
    TASKS = "followup_tasks_total"
    DECISIONS = "followup_decisions_total"
    ACTIONS = "followup_actions_total"
    DELIVERY_ATTEMPTS = "followup_delivery_attempts_total"
    LLM_REQUESTS = "followup_llm_requests_total"
    LLM_TOKENS = "followup_llm_tokens_total"
    LLM_COST = "followup_llm_cost_usd_total"
    AGENT_PLANS = "followup_agent_plans_total"
    SECURITY_EVENTS = "followup_security_events_total"


class HistogramName(StrEnum):
    """Histograms of the catalogue in ``docs/14`` section 8."""

    RUN_DURATION = "followup_run_duration_seconds"
    LLM_LATENCY = "followup_llm_latency_seconds"


class Telemetry(Protocol):
    """Record counters and durations; implementations must not raise."""

    def count(
        self, name: CounterName, amount: float = 1, **labels: str
    ) -> None:
        """Add ``amount`` to a counter series."""
        ...

    def observe(
        self, name: HistogramName, value: float, **labels: str
    ) -> None:
        """Record one observation of a histogram series."""
        ...


class NullTelemetry:
    """Default for tests and callers that do not export metrics."""

    def count(
        self, name: CounterName, amount: float = 1, **labels: str
    ) -> None:
        """Discard the sample."""

    def observe(
        self, name: HistogramName, value: float, **labels: str
    ) -> None:
        """Discard the sample."""


NULL_TELEMETRY = NullTelemetry()
