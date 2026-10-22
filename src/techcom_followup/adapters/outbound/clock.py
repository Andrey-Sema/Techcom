"""UTC system clock and controllable test clock."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from techcom_followup.application.repository import require_aware


class SystemClock:
    """Read UTC wall time only at the application boundary."""

    @staticmethod
    def now() -> datetime:
        """Return an aware UTC instant."""
        return datetime.now(UTC)

    def today(self) -> date:
        """Return the current UTC date."""
        return self.now().date()


@dataclass(slots=True)
class FixedClock:
    """Mutable test clock; assignments are validated when read."""

    instant: datetime

    def now(self) -> datetime:
        """Return the configured instant converted to UTC."""
        require_aware(self.instant)
        return self.instant.astimezone(UTC)

    def today(self) -> date:
        """Return the configured UTC date."""
        return self.now().date()
