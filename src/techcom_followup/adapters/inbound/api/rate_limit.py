"""Bounded subject limits shared across token rotations."""

from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from techcom_followup.adapters.inbound.api.middleware import ApiSettings


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """A guard for one event loop; share across workers in prod."""

    def __init__(
        self, settings: ApiSettings, monotonic: Callable[[], float]
    ) -> None:
        """Bound stored actor hashes without retaining raw tokens."""
        self._settings, self._monotonic = settings, monotonic
        self._buckets: dict[str, _Bucket] = {}

    @property
    def retry_after(self) -> str:
        """Seconds until a fresh token can be available."""
        return str(
            max(
                1,
                ceil(
                    self._settings.rate_refill_s / self._settings.rate_capacity
                ),
            )
        )

    def allow(self, actor_id: str) -> bool:
        """Refill buckets without evicting active subjects."""
        now = self._monotonic()
        settings = self._settings
        self._buckets = {
            key: bucket
            for key, bucket in self._buckets.items()
            if now - bucket.updated < settings.rate_refill_s
        }
        bucket = self._buckets.get(actor_id)
        if bucket is None:
            if len(self._buckets) >= settings.max_rate_subjects:
                return False
            bucket = _Bucket(float(settings.rate_capacity), now)
            self._buckets[actor_id] = bucket
        elapsed = max(0, now - bucket.updated)
        bucket.tokens = min(
            float(settings.rate_capacity),
            bucket.tokens
            + elapsed * settings.rate_capacity / settings.rate_refill_s,
        )
        bucket.updated = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True
