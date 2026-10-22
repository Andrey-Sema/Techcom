"""Process-local and PostgreSQL session advisory run locks."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class InMemoryRunLock:
    """Nonblocking shared lock for deterministic single-process runs."""

    def __init__(self) -> None:
        """Start unlocked; share this instance across processors."""
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        """Report contention and release on cancellation."""
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
            ready = False
            acquired = False
            try:
                acquired = bool(
                    await connection.scalar(
                        text("SELECT pg_try_advisory_lock(7482139071)")
                    )
                )
                await connection.commit()
                ready = True
                yield acquired
            finally:
                if not ready:
                    # The server may own the lock even if its response
                    # was lost. Never return that session to the pool.
                    await asyncio.shield(connection.invalidate())
                elif acquired:
                    try:
                        await asyncio.shield(_unlock(connection))
                    except BaseException:
                        await asyncio.shield(connection.invalidate())
                        raise


async def _unlock(connection: AsyncConnection) -> None:
    await connection.execute(text("SELECT pg_advisory_unlock(7482139071)"))
    await connection.commit()
