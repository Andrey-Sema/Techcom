"""Single-process receiver with fsync-before-acknowledgement semantics.

An asyncio lock protects reservations and commits. The journal is the
recovery source; run one mock worker for each journal. This is a local
test receiver, not a multi-process production delivery service.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from techcom_mocks.models import DeliveryEntry, DeliveryPayload, Receipt


class PayloadMismatchError(Exception):
    """An operation ID was reused for different message content."""


class InProgressError(Exception):
    """Another request already owns the operation reservation."""


class StoreUnavailableError(Exception):
    """A journal failure makes the delivery outcome uncertain."""


def payload_digest(payload: DeliveryPayload) -> str:
    """Hash canonical validated JSON; source key order is irrelevant."""
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class DeliveryStore:
    """Reserve each key atomically and recover committed receipts."""

    def __init__(self, journal: Path | None = None) -> None:
        """Replay a journal; malformed records fail startup."""
        self._journal = journal
        self._lock = asyncio.Lock()
        self._entries: dict[UUID, DeliveryEntry] = {}
        self._pending: set[UUID] = set()
        self._count = 0
        self._healthy = True
        if journal is not None and journal.exists():
            for line in journal.read_text(encoding="utf-8").splitlines():
                entry = DeliveryEntry.model_validate_json(line)
                if payload_digest(entry.payload) != entry.payload_sha256:
                    msg = "delivery journal digest mismatch"
                    raise ValueError(msg)
                self._entries[entry.receipt.operation_id] = entry
                self._count += 1

    @property
    def count(self) -> int:
        """Committed deliveries, including non-idempotent ones."""
        return self._count

    def _require_healthy(self) -> None:
        if not self._healthy:
            msg = "delivery journal unavailable; restart and reconcile"
            raise StoreUnavailableError(msg)

    async def deliver(
        self,
        operation_id: UUID,
        payload: DeliveryPayload,
        *,
        idempotent: bool = True,
    ) -> tuple[Receipt, bool]:
        """Commit once or return a receipt replay without writing again.

        Cancellation before commit releases the reservation. Once the
        synchronous commit starts, it completes before cancellation can
        be delivered. Any I/O error fails closed until journal recovery.
        """
        digest = payload_digest(payload)
        async with self._lock:
            self._require_healthy()
            previous = self._entries.get(operation_id)
            if previous is not None and idempotent:
                if previous.payload_sha256 != digest:
                    raise PayloadMismatchError
                return previous.receipt, True
            if operation_id in self._pending:
                raise InProgressError
            self._pending.add(operation_id)
        try:
            # Let competing requests observe the reservation.
            await asyncio.sleep(0)
            async with self._lock:
                self._require_healthy()
                entry = DeliveryEntry(
                    payload_sha256=digest,
                    payload=payload,
                    receipt=Receipt(
                        operation_id=operation_id,
                        provider_ref=f"mock-{self._count + 1}",
                    ),
                )
                self._commit(entry)
                return entry.receipt, False
        finally:
            self._pending.discard(operation_id)

    def _commit(self, entry: DeliveryEntry) -> None:
        if self._journal is not None:
            try:
                with self._journal.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            entry.model_dump(mode="json"), ensure_ascii=True
                        )
                        + "\n"
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                self._healthy = False
                msg = "delivery journal commit failed"
                raise StoreUnavailableError(msg) from exc
        self._entries[entry.receipt.operation_id] = entry
        self._count += 1

    async def lookup(self, operation_id: UUID) -> Receipt | None:
        """Find a receipt; uncertain writes never become 404."""
        async with self._lock:
            self._require_healthy()
            entry = self._entries.get(operation_id)
            return None if entry is None else entry.receipt
