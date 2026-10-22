import asyncio
import signal
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from techcom_followup.adapters.inbound import signals


async def test_completed_command_removes_installed_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    register, remove = (Mock(), Mock())
    monkeypatch.setattr(loop, "add_signal_handler", register)
    monkeypatch.setattr(loop, "remove_signal_handler", remove)

    async def command() -> str:
        return "finished"

    stop = Mock()
    assert await signals.run_with_shutdown(command, stop) == "finished"
    register.assert_called_once()
    assert register.call_args.args[0] == signal.SIGTERM
    remove.assert_called_once_with(signal.SIGTERM)
    stop.assert_not_called()


async def test_non_posix_completion_uses_cancellation_without_signal_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", Mock(side_effect=NotImplementedError)
    )
    remove = Mock()
    monkeypatch.setattr(loop, "remove_signal_handler", remove)

    async def command() -> int:
        return 42

    assert await signals.run_with_shutdown(command, Mock()) == 42
    remove.assert_not_called()


@pytest.mark.parametrize("expires", [False, True])
async def test_sigterm_drains_owned_work_and_classifies_intentional_stop(
    monkeypatch: pytest.MonkeyPatch, expires: bool
) -> None:
    loop = asyncio.get_running_loop()
    callbacks: list[Callable[[], None]] = []
    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda _sig, callback: callbacks.append(callback),
    )
    monkeypatch.setattr(loop, "remove_signal_handler", Mock())
    entered, released = (asyncio.Event(), asyncio.Event())
    stop = Mock()

    async def command() -> str:
        entered.set()
        await released.wait()
        return "acknowledged"

    async def drain(
        worker: asyncio.Task[object], *, stop_claiming: Callable[[], None]
    ) -> None:
        stop_claiming()
        if expires:
            worker.cancel()
        else:
            released.set()
        await asyncio.gather(worker, return_exceptions=True)

    monkeypatch.setattr(signals, "graceful_shutdown", drain)
    task = asyncio.create_task(signals.run_with_shutdown(command, stop))
    await entered.wait()
    callbacks[0]()
    if expires:
        with pytest.raises(signals.GracefulStopError):
            await task
    else:
        assert await task == "acknowledged"
    stop.assert_called_once()


async def test_external_cancellation_waits_for_worker_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", Mock())
    monkeypatch.setattr(loop, "remove_signal_handler", Mock())
    entered, cleaned = (asyncio.Event(), asyncio.Event())
    stop = Mock()

    async def command() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def drain(
        worker: asyncio.Task[object], *, stop_claiming: Callable[[], None]
    ) -> None:
        stop_claiming()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    monkeypatch.setattr(signals, "graceful_shutdown", drain)
    task = asyncio.create_task(signals.run_with_shutdown(command, stop))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    stop.assert_called_once()


async def test_dependency_cancelled_error_propagates_without_second_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", Mock())
    monkeypatch.setattr(loop, "remove_signal_handler", Mock())

    async def command() -> None:
        raise asyncio.CancelledError

    stop = Mock()
    with pytest.raises(asyncio.CancelledError):
        await signals.run_with_shutdown(command, stop)
    stop.assert_not_called()
