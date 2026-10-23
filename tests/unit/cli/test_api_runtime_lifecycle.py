"""Listener ownership, startup admission and lazy provider setup."""

import asyncio
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from api_support import Harness
from support import AS_OF
from techcom_followup import serving
from techcom_followup.adapters.inbound.api.app import create_app
from techcom_followup.application.process_tasks import ProcessTasks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class ListenerProbe:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    should_exit: bool = False
    stopped: bool = False

    async def serve(self) -> None:
        self.started.set()
        try:
            while True:
                if self.should_exit or self.release.is_set():
                    break
                await asyncio.sleep(0)
        finally:
            self.stopped = True


@pytest.mark.parametrize("mode", ["signal", "exit", "cancel"])
async def test_every_listener_is_joined_on_shutdown(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks: dict[signal.Signals, Callable[[], None]] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        callbacks.__setitem__,
    )
    monkeypatch.setattr(
        loop, "remove_signal_handler", lambda sig: callbacks.pop(sig, None)
    )
    probes = [ListenerProbe(), ListenerProbe()]
    stopped: list[bool] = []
    task = asyncio.create_task(
        serving._run_listeners(
            [cast("serving._Listener", probe) for probe in probes],
            lambda: stopped.append(True),
        )
    )
    try:
        async with asyncio.timeout(5):
            await asyncio.gather(*(probe.started.wait() for probe in probes))
            if mode == "signal":
                callbacks[signal.SIGTERM]()
            elif mode == "exit":
                probes[0].release.set()
            else:
                task.cancel()
            if mode == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
        assert stopped
        assert all(probe.stopped and probe.should_exit for probe in probes)
        assert not callbacks
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("allowed", [True, False])
async def test_startup_checks_close_the_connection_when_permission_is_missing(
    allowed: bool,
) -> None:
    closed: list[bool] = []

    @dataclass
    class Result:
        def scalar_one(self) -> bool:
            return allowed

    class Connection:
        async def execute(self, _statement: object) -> Result:
            return Result()

    class Engine:
        @asynccontextmanager
        async def connect(self) -> AsyncIterator[Connection]:
            try:
                yield Connection()
            finally:
                closed.append(True)

    if allowed:
        await serving._check_storage(cast("AsyncEngine", Engine()))
    else:
        with pytest.raises(ValueError, match="admission permission"):
            await serving._check_storage(cast("AsyncEngine", Engine()))
    assert closed == [True]


async def test_reads_skip_provider_init_and_trigger_errors_stay_private() -> (
    None
):
    calls: list[bool] = []

    def unavailable_processor() -> ProcessTasks:
        calls.append(True)
        message = "PRIVATE provider configuration"
        raise RuntimeError(message)

    harness = Harness()
    harness.app = create_app(
        repository=harness.repo,
        runner=serving._Runs(unavailable_processor),
        verifier=harness.verifier,
        clock=harness.clock,
    )
    async with harness.client(harness.token()) as client:
        assert (await client.get("/v1/actions")).status_code == 200
        assert not calls
        response = await client.post(
            "/v1/runs", json={"as_of": AS_OF.isoformat()}
        )
        assert response.status_code == 500
        assert "PRIVATE" not in response.text
    assert calls == [True]
