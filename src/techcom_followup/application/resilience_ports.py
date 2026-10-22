"""Admission receipts shared across resilience implementations."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BreakerPermit:
    """Bind completion to a breaker generation and attempt."""

    owner: object
    generation: int
