"""Own command tasks until completion or bounded signal-driven drain."""

import asyncio
import signal
from collections.abc import Awaitable, Callable

from techcom_followup.application.lifecycle import graceful_shutdown


class GracefulStopError(Exception):
    """An intentional stop completed after its bounded drain period."""


async def run_with_shutdown[Result](
    operation: Callable[[], Awaitable[Result]],
    stop_claiming: Callable[[], None],
) -> Result:
    """Drain on SIGTERM before closing clients."""
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, stopped.set)
        installed = True
    except NotImplementedError:
        # Non-POSIX loops still support cancellation-driven drain.
        pass

    async def invoke() -> Result:
        return await operation()

    worker = asyncio.create_task(invoke())
    requested = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait(
            {worker, requested}, return_when=asyncio.FIRST_COMPLETED
        )
        if worker not in done:
            await graceful_shutdown(worker, stop_claiming=stop_claiming)
            if worker.cancelled():
                raise GracefulStopError
        return await worker
    except asyncio.CancelledError:
        if not worker.done():
            await asyncio.shield(
                graceful_shutdown(worker, stop_claiming=stop_claiming)
            )
        raise
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)
        requested.cancel()
        await asyncio.gather(requested, return_exceptions=True)
