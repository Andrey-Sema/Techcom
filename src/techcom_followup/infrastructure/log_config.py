"""Allow-listed JSON logging without notes or credentials."""

import math
import re
import sys
from uuid import UUID

import structlog
from structlog.typing import EventDict, WrappedLogger

_EVENTS = frozenset(
    {
        "log.redacted",
        "command.completed",
        "command.failed",
        "llm.call",
        "llm.route_failover",
        "llm.unavailable",
        "llm.budget_exhausted",
        "llm.validation_failed",
    }
)
_COUNTERS = frozenset(
    {
        "tokens_in",
        "tokens_out",
        "tokens_cached",
        "tokens_reasoning",
        "attempt_count",
        "enqueued",
        "duplicates",
        "delivered",
    }
)
_CLOSED_FIELDS = {
    "stage": frozenset(
        {
            "size",
            "json",
            "schema",
            "confidence",
            "ungrounded",
            "transport",
            "unapproved_phrasing",
            "message_rules",
        }
    ),
    "prompt_version": frozenset({"interp.v1", "draft.v2"}),
}


def _validation_metadata(event: EventDict, safe: EventDict) -> None:
    for key, allowed in _CLOSED_FIELDS.items():
        value = event.get(key)
        if isinstance(value, str) and value in allowed:
            safe[key] = value
    digest = event.get("output_sha256")
    if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
        safe["output_sha256"] = digest


def _counter_metadata(event: EventDict, safe: EventDict) -> None:
    for key in _COUNTERS:
        value = event.get(key)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            safe[key] = value


def _run_metadata(event: EventDict, safe: EventDict) -> None:
    run_id = event.get("run_id")
    if isinstance(run_id, UUID):
        safe["run_id"] = str(run_id)
    latency = event.get("latency_s")
    if isinstance(latency, float) and math.isfinite(latency) and latency >= 0:
        safe["latency_s"] = latency


def redact_event(
    _logger: WrappedLogger, method: str, event: EventDict
) -> EventDict:
    """Keep a closed event name, generated identity and counters."""
    name = event.get("event")
    safe: EventDict = {
        "event": name
        if isinstance(name, str) and name in _EVENTS
        else "log.redacted",
        "level": method
        if method in {"debug", "info", "warning", "error", "critical"}
        else "info",
    }
    _counter_metadata(event, safe)
    _run_metadata(event, safe)
    if safe["event"] == "llm.validation_failed":
        _validation_metadata(event, safe)
    return safe


def configure_logging() -> None:
    """Write one escaped JSON record per event to standard error."""
    structlog.configure(
        processors=[
            redact_event,
            structlog.processors.JSONRenderer(ensure_ascii=True),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
