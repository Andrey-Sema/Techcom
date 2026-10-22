import asyncio

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from techcom_followup.adapters.outbound.run_lock import PgRunLock

pytestmark = pytest.mark.integration


async def test_advisory_lock_excludes_another_connection_and_unlocks(
    pg_engine: AsyncEngine,
) -> None:
    first, second = PgRunLock(pg_engine), PgRunLock(pg_engine)
    async with first.acquire() as acquired:
        assert acquired
        async with second.acquire() as contended:
            assert not contended
    async with second.acquire() as acquired:
        assert acquired


async def test_advisory_lock_releases_after_run_cancellation(
    pg_engine: AsyncEngine,
) -> None:
    lock = PgRunLock(pg_engine)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> None:
        async with lock.acquire() as acquired:
            assert acquired
            entered.set()
            await release.wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with lock.acquire() as acquired:
        assert acquired


@pytest.mark.parametrize("stage", ["acquisition", "commit", "unlock"])
async def test_advisory_lock_never_returns_an_uncertain_session_to_pool(
    pg_engine: AsyncEngine,
    stage: str,
) -> None:

    pooled = create_async_engine(pg_engine.url, pool_size=1, max_overflow=0)
    fired = False

    def cancel_once(*arguments: object) -> None:
        nonlocal fired
        if stage == "unlock":
            statement = arguments[2]
            if (
                not isinstance(statement, str)
                or "pg_advisory_unlock" not in statement
            ):
                return
        if not fired:
            fired = True
            raise asyncio.CancelledError

    event_name = {
        "acquisition": "after_cursor_execute",
        "commit": "commit",
        "unlock": "before_cursor_execute",
    }[stage]
    event.listen(pooled.sync_engine, event_name, cancel_once)
    try:
        with pytest.raises(asyncio.CancelledError):
            async with PgRunLock(pooled).acquire() as acquired:
                assert acquired
        assert fired
        # NullPool guarantees a different physical session.
        async with PgRunLock(pg_engine).acquire() as acquired:
            assert acquired
    finally:
        event.remove(pooled.sync_engine, event_name, cancel_once)
        await pooled.dispose()
