"""Source ordering, separate from calendar dates and action identity."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """An aware source instant, or an explicitly coarse date.

    A date cannot establish order against another observation that day.
    Equal or incomparable versions retain arrival order; they are not
    evidence that a task was unchanged.
    """

    updated_at: datetime
    date_only: bool = False

    def __post_init__(self) -> None:
        """Reject timestamps that cannot be compared across workers."""
        if self.updated_at.utcoffset() is None:
            message = "source version must be timezone-aware"
            raise ValueError(message)
        object.__setattr__(self, "updated_at", self.updated_at.astimezone(UTC))

    def older_than(self, other: Self) -> bool:
        """Compare source order, respecting coarse dates."""
        if self.updated_at.date() != other.updated_at.date():
            return self.updated_at.date() < other.updated_at.date()
        return (
            not self.date_only
            and not other.date_only
            and self.updated_at < other.updated_at
        )

    def advances(self, other: Self | None) -> bool:
        """Advance without losing precision within a day."""
        if other is None:
            return True
        if self.updated_at.date() != other.updated_at.date():
            return self.updated_at.date() > other.updated_at.date()
        return not self.date_only and (
            other.date_only or self.updated_at > other.updated_at
        )
