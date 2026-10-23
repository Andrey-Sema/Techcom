"""The API composition root wires shared limits, logs and listeners."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from pydantic import SecretStr

from techcom_followup import serving
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.application.command_ports import CommandOptions
from techcom_followup.bootstrap import RuntimeSettings


def api_settings(tmp_path: Path) -> serving.ApiRuntimeSettings:
    pem = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"k1": pem.decode("ascii")}))
    return serving.ApiRuntimeSettings(
        issuer="issuer",
        audience="audience",
        public_keys_file=path,
        otlp_endpoint="http://alloy:4318/v1/logs",
        allow_internal_http=True,
    )


@dataclass
class Services:
    repository: InMemoryRepository = field(default_factory=InMemoryRepository)
    stopped: list[bool] = field(default_factory=list)

    def processor(self) -> object:
        message = "processing is not part of this test"
        raise AssertionError(message)

    def stop_claiming(self) -> None:
        self.stopped.append(True)


def test_listeners_keep_metrics_on_a_separate_bounded_socket(
    tmp_path: Path,
) -> None:
    api = api_settings(tmp_path)
    verifier = serving.token_verifier(api)
    assert verifier is not None
    public, metrics = serving._listeners(Mock(), Mock(), api)
    assert (public.config.port, metrics.config.port) == (8080, 9464)
    assert not public.config.access_log
    assert not public.config.proxy_headers
    with public.capture_signals():
        pass
    assert serving.ApiClock.monotonic() > 0


async def test_api_requires_persistent_storage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="persistent PostgreSQL"):
        await serving.serve(RuntimeSettings(), api_settings(tmp_path))


async def test_serve_composes_shared_limits_and_disposes_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(
        serving, "create_async_engine", Mock(return_value=engine)
    )
    monkeypatch.setattr(serving, "_check_storage", AsyncMock())
    services = Services()
    options: list[object] = []

    def builder(_settings: RuntimeSettings) -> Callable[..., object]:
        @asynccontextmanager
        async def build(option: object) -> AsyncIterator[Services]:
            options.append(option)
            yield services

        return build

    monkeypatch.setattr(serving, "RuntimeBuilder", builder)
    served: list[int] = []

    async def run_listeners(
        listeners: list[serving._Listener], stop: Callable[[], None]
    ) -> None:
        served.append(len(listeners))
        stop()
        await asyncio.sleep(0.15)

    monkeypatch.setattr(serving, "_run_listeners", run_listeners)
    await serving.serve(
        RuntimeSettings(
            database_url=SecretStr("postgresql+psycopg://u:p@db/db")
        ),
        api_settings(tmp_path),
    )
    assert served == [2]
    assert services.stopped == [True]
    assert len(options) == 1
    assert isinstance(options[0], CommandOptions)
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("secrets_dir", [False, True])
def test_main_hides_startup_failure_details(
    secrets_dir: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "techcom_followup.serving.Path",
        lambda _path: Mock(is_dir=lambda: secrets_dir),
    )
    captured: list[object] = []

    def fail(coroutine: object) -> None:
        captured.append(coroutine)
        coroutine.close()  # type: ignore[attr-defined]
        message = "PRIVATE detail"
        raise RuntimeError(message)

    monkeypatch.setattr("techcom_followup.serving.asyncio.run", fail)
    monkeypatch.setattr(serving, "RuntimeSettings", Mock())
    monkeypatch.setattr(serving, "ApiRuntimeSettings", Mock())
    with pytest.raises(SystemExit) as caught:
        serving.main()
    assert "PRIVATE" not in str(caught.value)
    assert captured


def test_main_returns_after_orderly_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def finish(coroutine: object) -> None:
        coroutine.close()  # type: ignore[attr-defined]

    monkeypatch.setattr("techcom_followup.serving.asyncio.run", finish)
    monkeypatch.setattr(serving, "RuntimeSettings", Mock())
    monkeypatch.setattr(serving, "ApiRuntimeSettings", Mock())
    serving.main()
