import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr

from support import AS_OF, ROOT
from techcom_followup import bootstrap
from techcom_followup.adapters.outbound.clock import FixedClock, SystemClock
from techcom_followup.adapters.outbound.llm_stub import ScriptedLLM
from techcom_followup.application.command_ports import CommandOptions
from techcom_followup.infrastructure.http_client import UpstreamError


@pytest.mark.parametrize("records", [{}, [0] * 10_001])
async def test_fixture_shape_and_record_count_are_bounded(
    tmp_path: Path, records: object
) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(UpstreamError, match="fixture_shape"):
        await bootstrap.FixtureTaskSource(path).fetch_tasks()


async def test_fixture_byte_limit_is_checked_before_json_parsing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixture.json"
    path.write_bytes(b" " * 2_000_001)
    with pytest.raises(UpstreamError, match="fixture_size"):
        await bootstrap.FixtureTaskSource(path).fetch_tasks()


async def test_database_engine_is_owned_and_closed_even_on_command_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Mock(dispose=AsyncMock())
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(bootstrap, "create_async_engine", create_engine)
    settings = bootstrap.RuntimeSettings(
        database_url=SecretStr("postgresql+psycopg://private:secret@db/db")
    )

    async def fail_command() -> None:
        async with bootstrap.RuntimeBuilder(settings)(
            CommandOptions(AS_OF)
        ) as services:
            assert services.persistent
            # Reports do not need policy or credentials.
            services.stop_claiming()
            message = "command failed"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="command failed"):
        await fail_command()
    assert settings.database_url is not None
    create_engine.assert_called_once_with(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=settings.write_concurrency + 2,
        max_overflow=0,
        pool_timeout=5,
        isolation_level="READ COMMITTED",
        connect_args={
            "options": "-c statement_timeout=15000 -c lock_timeout=5000"
        },
    )
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("has_directory", [False, True])
def test_secret_directory_is_optional_without_warning(
    monkeypatch: pytest.MonkeyPatch, has_directory: bool
) -> None:
    monkeypatch.setattr(Path, "is_dir", Mock(return_value=has_directory))
    settings = bootstrap.RuntimeSettings()
    constructor = Mock(return_value=settings)
    monkeypatch.setattr(bootstrap, "RuntimeSettings", constructor)
    assert bootstrap._load_settings() is settings
    constructor.assert_called_once_with(
        _secrets_dir=Path("/run/secrets") if has_directory else None
    )


@pytest.mark.parametrize("fixed", [False, True])
def test_demo_advance_never_changes_system_clock(fixed: bool) -> None:
    instant = datetime(2026, 10, 21, tzinfo=UTC)
    clock = FixedClock(instant) if fixed else SystemClock()
    bootstrap._advance(clock, timedelta(seconds=20))
    if isinstance(clock, FixedClock):
        assert clock.now() == instant + timedelta(seconds=20)


async def test_router_runtime_closes_sdk_and_shares_policy_horizon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_records: list[object],
) -> None:
    for name in ("policy.toml", "routing.toml", "llm.toml"):
        content = (ROOT / "config" / name).read_text()
        if name == "policy.toml":
            content = content.replace("due_soon_days = 1", "due_soon_days = 5")
        (tmp_path / name).write_text(content, encoding="utf-8")
    assert isinstance(fixture_records[0], dict)
    task = fixture_records[0] | {
        "status": "Working on it",
        "due_date": "2026-10-23",
        "notes": "The next crew is not ready.",
    }
    fixture = tmp_path / "tasks.json"
    fixture.write_text(json.dumps([task]), encoding="utf-8")
    language = ScriptedLLM(
        [
            json.dumps(
                {
                    "blocker": "upstream_dependency",
                    "confidence": 0.9,
                    "needs_eta": False,
                    "evidence": "next crew is not ready",
                }
            )
        ]
    )
    sdk = Mock(close=AsyncMock())
    monkeypatch.setattr(bootstrap, "load_openai_settings", Mock())
    monkeypatch.setattr(
        bootstrap, "build_openai_client", Mock(return_value=sdk)
    )
    build = Mock(return_value=language)
    monkeypatch.setattr(bootstrap, "build_router", build)
    settings = bootstrap.RuntimeSettings(
        config_dir=tmp_path, llm_provider="router"
    )
    async with bootstrap.RuntimeBuilder(settings)(
        CommandOptions(AS_OF, fixture)
    ) as services:
        first = services.processor()
        assert services.processor() is first
        result = await first.execute(as_of=AS_OF)
        assert result.status == "completed"
        # A horizon of 1 would skip this ambiguous note due in two days.
        assert language.requests[0].purpose.value == "interpret_note"
        assert result.decisions[0].interpretation is not None
        assert (
            result.decisions[0].interpretation.blocker.value
            == "upstream_dependency"
        )
    sdk.close.assert_awaited_once()
    assert build.call_args.kwargs["today"]() == SystemClock().today()


async def test_language_cache_namespace_changes_with_route_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = (ROOT / "config/llm.toml").read_text()
    (tmp_path / "llm.toml").write_text(config, encoding="utf-8")
    monkeypatch.setattr(bootstrap, "load_openai_settings", Mock())
    monkeypatch.setattr(
        bootstrap,
        "build_openai_client",
        Mock(return_value=Mock(close=AsyncMock())),
    )
    monkeypatch.setattr(
        bootstrap, "build_router", Mock(return_value=ScriptedLLM())
    )
    settings = bootstrap.RuntimeSettings(
        config_dir=tmp_path, llm_provider="router"
    )
    async with AsyncExitStack() as stack:
        _, first = bootstrap._language(stack, settings, SystemClock())
        (tmp_path / "llm.toml").write_text(
            config.replace('daily_budget_usd = "5"', 'daily_budget_usd = "6"'),
            encoding="utf-8",
        )
        _, second = bootstrap._language(stack, settings, SystemClock())
    assert first != second


def test_entrypoint_installs_logging_and_calls_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure, command = Mock(), Mock()
    monkeypatch.setattr(bootstrap, "configure_logging", configure)
    factory = Mock(return_value=command)
    monkeypatch.setattr(bootstrap, "create_cli", factory)
    bootstrap.main()
    configure.assert_called_once()
    assert isinstance(factory.call_args.args[0], bootstrap.RuntimeBuilder)
    command.assert_called_once()


async def test_demo_disables_environment_proxies_without_making_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(variable, "http://untrusted-proxy.example:8080")
    for variable in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(variable, "")
    constructor = Mock(wraps=httpx.AsyncClient)
    monkeypatch.setattr(httpx, "AsyncClient", constructor)
    async with bootstrap.RuntimeBuilder(bootstrap.RuntimeSettings())(
        CommandOptions(AS_OF, demo=True)
    ) as services:
        services.dispatcher()
        constructor.assert_called_once_with(trust_env=False)
