"""Process-local and PostgreSQL session advisory run locks."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class InMemoryRunLock:
    """Nonblocking shared lock for deterministic single-process runs."""

    def __init__(self) -> None:
        """Start unlocked; share this instance across processors."""
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        """Yield false on contention and always release on."""
        if self._lock.locked():
            yield False
            return
        await self._lock.acquire()
        try:
            yield True
        finally:
            self._lock.release()


class PgRunLock:
    """Hold a dedicated connection for a session advisory lock.

    Repository transactions use other connections; network waits never
    hold a database transaction. Pool return follows explicit unlock.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Keep engine ownership with the composition root."""
        self._engine = engine

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        """Try the fixed application lock, releasing on every exit."""
        async with self._engine.connect() as connection:
            acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(7482139071)")
                )
            )
            await connection.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    await asyncio.shield(
                        connection.execute(
                            text("SELECT pg_advisory_unlock(7482139071)")
                        )
                    )
                    await connection.commit()
