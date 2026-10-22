"""Bounded retry arithmetic and per-cycle retry admission."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential full jitter with bounded server-directed delays."""

    max_attempts: int = 6
    base_seconds: float = 2
    cap_seconds: float = 300

    def __post_init__(self) -> None:
        """Reject invalid retry configuration before any I/O."""
        if (
            self.max_attempts < 1
            or not isfinite(self.base_seconds)
            or not isfinite(self.cap_seconds)
            or not 0 < self.base_seconds <= self.cap_seconds
        ):
            message = "retry bounds must be finite and positive"
            raise ValueError(message)

    def delay(
        self, attempt: int, *, jitter: float, retry_after: float = 0
    ) -> float:
        """Return a capped delay; jitter is an injected unit."""
        if attempt < 1 or not 0 <= jitter <= 1:
            message = "attempt must be positive and jitter in [0, 1]"
            raise ValueError(message)
        server = retry_after if isfinite(retry_after) else 0
        exponent = min(attempt - 1, 32)
        ceiling = min(self.cap_seconds, self.base_seconds * float(2**exponent))
        return min(self.cap_seconds, max(ceiling * jitter, server, 0))


class RetryBudget:
    """Spend at most twenty percent of a known request allocation.

    Share one instance for the operation's entire cycle. Initial
    attempts never consume tokens; each extra network attempt does.
    """

    def __init__(self, requests: int) -> None:
        """Allocate floor(requests / 5) retries, without overdrafts."""
        if requests < 0:
            message = "request allocation cannot be negative"
            raise ValueError(message)
        self._remaining = requests // 5

    @property
    def remaining(self) -> int:
        """Return unspent retry tokens."""
        return self._remaining

    def take(self) -> bool:
        """Atomically admit a retry within one event-loop thread."""
        if not self._remaining:
            return False
        self._remaining -= 1
        return True
