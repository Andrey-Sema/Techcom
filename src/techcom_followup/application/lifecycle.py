"""Bounded shutdown orchestration for inbound signal adapters."""

import asyncio
from collections.abc import Callable

MAX_SHUTDOWN_SECONDS = 25


async def graceful_shutdown[Result](
    worker: asyncio.Task[Result],
    *,
    stop_claiming: Callable[[], None],
    timeout_seconds: float = 25,
) -> None:
    """Stop admission, drain the current operation, then cancel it.

    Dispatch cancellation releases its fenced claim into unknown;
    a cancelled write must be reconciled before another send.
    """
    if not 0 < timeout_seconds <= MAX_SHUTDOWN_SECONDS:
        message = "shutdown grace must be in (0, 25] seconds"
        raise ValueError(message)
    stop_claiming()
    done, _ = await asyncio.wait({worker}, timeout=timeout_seconds)
    if not done:
        worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
