import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import structlog
import typer
from pydantic import SecretStr
from typer.testing import CliRunner, Result

from support import AS_OF, ROOT
from techcom_followup import bootstrap
from techcom_followup.adapters.inbound import cli
from techcom_followup.adapters.inbound.signals import GracefulStopError
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.command_ports import (
    CommandOptions,
    CommandServices,
)
from techcom_followup.application.process_tasks import RunReport
from techcom_followup.infrastructure.log_config import configure_logging
from techcom_mocks.app import create_app
from techcom_mocks.faults import FaultInjectingTransport, FaultPlan
from techcom_mocks.store import DeliveryStore

type Receiver = tuple[
    Callable[[str], None],
    DeliveryStore,
    list[httpx.AsyncClient],
    list[httpx.Request],
]


@pytest.fixture(autouse=True)
def json_logs() -> Iterator[None]:
    previous = structlog.get_config()
    configure_logging()
    yield
    structlog.configure(**previous)


@pytest.fixture
def http_receiver(
    monkeypatch: pytest.MonkeyPatch, fixture_records: list[object]
) -> Receiver:
    store = DeliveryStore()
    app = create_app(tasks=fixture_records, store=store)
    original = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []
    requests: list[httpx.Request] = []

    def install(faults: str = "") -> None:

        async def observe(request: httpx.Request) -> None:
            requests.append(request)

        def build(*, trust_env: bool = True) -> httpx.AsyncClient:
            client = original(
                trust_env=trust_env,
                transport=FaultInjectingTransport(
                    httpx.ASGITransport(app=app), FaultPlan.parse(faults)
                ),
                event_hooks={"request": [observe]},
            )
            clients.append(client)
            return client

        monkeypatch.setattr(httpx, "AsyncClient", build)

    install()
    return (install, store, clients, requests)


def app(settings: bootstrap.RuntimeSettings | None = None) -> typer.Typer:
    return cli.create_cli(
        bootstrap.RuntimeBuilder(
            settings or bootstrap.RuntimeSettings(config_dir=ROOT / "config")
        )
    )


def report(result: Result) -> dict[str, Any]:
    assert result.exit_code == 0, (result.output, result.exception)
    return cast("dict[str, Any]", json.loads(result.stdout))


def test_run_exports_explainable_decisions_and_dispatches_once(
    tmp_path: Path, http_receiver: Receiver
) -> None:
    _, store, clients, requests = http_receiver
    result = CliRunner().invoke(
        app(),
        [
            "run",
            "--as-of",
            "2026-10-21",
            "--dispatch",
            "--output",
            str(tmp_path),
        ],
    )
    saved = report(result)
    assert saved["run"]["stats"]["decisions"] == 10
    assert len(saved["decisions"]) == 10
    assert len(saved["actions"]) == 7
    assert {action["status"] for action in saved["actions"]} == {"sent"}
    assert saved["storage"] == "memory"
    assert len(store._entries) == 7
    assert sum(request.method == "POST" for request in requests) == 7
    assert all(client.is_closed for client in clients)
    assert json.loads((tmp_path / "run_report.json").read_text()) == saved
    assert (
        json.loads((tmp_path / "decisions.json").read_text())
        == saved["decisions"]
    )
    assert (tmp_path / "run_report.json").stat().st_mode & 511 == 384
    assert saved["decisions"][0]["rule_id"] == "R7"
    assert saved["decisions"][0]["message"]["source"] == "template"


def test_dry_run_uses_fixture_and_needs_no_network_or_llm_credentials(
    tmp_path: Path, http_receiver: Receiver
) -> None:
    _, store, clients, requests = http_receiver
    settings = bootstrap.RuntimeSettings(
        config_dir=ROOT / "config", llm_provider="router"
    )
    saved = report(
        CliRunner().invoke(
            app(settings),
            [
                "run",
                "--fixture",
                str(ROOT / "fixtures/tasks.json"),
                "--as-of",
                "2026-10-21",
                "--dry-run",
                "--dispatch",
                "--output",
                str(tmp_path),
            ],
        )
    )
    assert saved["status"] == "dry_run"
    assert len(saved["decisions"]) == 10
    assert {decision["outcome"] for decision in saved["decisions"]} == {
        "planned"
    }
    assert (
        sum(decision["message"] is not None for decision in saved["decisions"])
        == 7
    )
    assert not store._entries
    assert not clients
    assert not requests


@pytest.mark.parametrize("faults", ["", "messages:timeout_after_commit@1"])
def test_demo_reconciles_ambiguous_send_and_rerun_has_no_duplicates(
    faults: str, http_receiver: Receiver
) -> None:
    install, store, clients, requests = http_receiver
    install(faults)
    saved = report(CliRunner().invoke(app(), ["demo"]))
    assert saved["demo"]["first_enqueued"] == 7
    assert saved["demo"]["rerun_duplicates"] == 7
    assert saved["demo"]["reconciled"] == bool(faults)
    assert len(store._entries) == 7
    assert sum(request.method == "POST" for request in requests) == 7
    assert {action["status"] for action in saved["actions"]} == {"sent"}
    assert all(client.is_closed for client in clients)


def test_demo_ignores_configured_remote_provider_and_database(
    http_receiver: Receiver,
) -> None:
    _, _, clients, requests = http_receiver
    settings = bootstrap.RuntimeSettings(
        config_dir=ROOT / "config",
        llm_provider="router",
        database_url=SecretStr(
            "postgresql+psycopg://private:secret@db/private"
        ),
        task_api_url="https://private.example",
        delivery_api_url="https://private.example",
        allowed_hosts=frozenset({"private.example"}),
        allow_local_http=False,
    )
    saved = report(CliRunner().invoke(app(settings), ["demo"]))
    assert saved["demo"]["first_enqueued"] == 7
    assert {request.url.host for request in requests} == {"127.0.0.1"}
    assert all(client.is_closed for client in clients)


def test_kill_switch_is_checked_without_delivery_io(
    monkeypatch: pytest.MonkeyPatch, http_receiver: Receiver
) -> None:
    _, store, _, requests = http_receiver
    monkeypatch.setenv("FOLLOWUP_DISPATCH_ENABLED", "false")
    saved = report(
        CliRunner().invoke(
            app(), ["run", "--as-of", "2026-10-21", "--dispatch"]
        )
    )
    assert {action["status"] for action in saved["actions"]} == {"pending"}
    assert not store._entries
    assert not any(request.method == "POST" for request in requests)


@pytest.mark.parametrize(
    "command",
    [["dispatch"], ["reconcile"], ["report", "--run-id", str(UUID(int=1))]],
)
def test_separate_commands_fail_clearly_without_persistence(
    command: list[str],
) -> None:
    result = CliRunner().invoke(app(), command)
    assert result.exit_code == 1
    assert "requires PostgreSQL" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        ["run", "--as-of", "PRIVATE"],
        ["demo", "--as-of", "PRIVATE"],
        ["report", "--run-id", "invalid"],
        ["dispatch", "--limit", "0"],
        ["reconcile", "--limit", "1001"],
    ],
)
def test_invalid_operator_arguments_are_rejected(command: list[str]) -> None:
    assert CliRunner().invoke(app(), command).exit_code == 2


def test_as_of_can_be_set_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOLLOWUP_AS_OF", "2026-10-21")
    saved = report(
        CliRunner().invoke(
            app(), ["run", "--fixture", str(ROOT / "fixtures/tasks.json")]
        )
    )
    assert saved["run"]["as_of"] == "2026-10-21"


def test_default_as_of_is_a_valid_utc_date() -> None:
    before = datetime.now(UTC).date()
    actual = cli._date(None)
    after = datetime.now(UTC).date()
    assert before <= actual <= after


def test_dependency_errors_do_not_print_secret_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "private.json"
    fixture.write_text("PRIVATE-NOTE-SECRET", encoding="utf-8")
    result = CliRunner().invoke(app(), ["run", "--fixture", str(fixture)])
    assert result.exit_code == 1
    assert result.stderr.strip() == "Command failed."
    assert "PRIVATE-NOTE-SECRET" not in capsys.readouterr().err


def test_failed_export_preserves_existing_file_and_removes_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    path.write_text("previous", encoding="utf-8")
    original = Path.replace

    def fail_replace(self: Path, target: Path) -> Path:
        if target == path:
            message = "disk failure"
            raise OSError(message)
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        cli._write_json(path, {"new": True})
    assert path.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [path]


def test_saved_report_and_recovery_only_construct_their_own_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryRepository()

    @asynccontextmanager
    async def factory(
        options: CommandOptions,
    ) -> AsyncIterator[CommandServices]:
        async with bootstrap.RuntimeBuilder(
            bootstrap.RuntimeSettings(
                config_dir=Path("missing"), llm_provider="router"
            )
        )(options) as services:
            yield replace(services, repository=repository, persistent=True)

    result = CliRunner().invoke(
        cli.create_cli(factory), ["report", "--run-id", str(UUID(int=1))]
    )
    assert result.exit_code == 1
    assert "requested run was not found" in result.stderr


@pytest.mark.parametrize("command", ["dispatch", "reconcile"])
def test_saved_recovery_commands_work_without_policy_or_language_config(
    command: str, http_receiver: Receiver
) -> None:

    @asynccontextmanager
    async def factory(
        options: CommandOptions,
    ) -> AsyncIterator[CommandServices]:
        async with bootstrap.RuntimeBuilder(
            bootstrap.RuntimeSettings(
                config_dir=Path("missing"), llm_provider="router"
            )
        )(options) as services:
            yield replace(services, persistent=True)

    assert report(
        CliRunner().invoke(cli.create_cli(factory), [command, "--limit", "1"])
    ) == {"processed": 0}


@pytest.mark.parametrize("command", ["run", "demo"])
def test_run_disappearing_before_report_is_safe_operator_error(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    http_receiver: Receiver,
) -> None:

    @asynccontextmanager
    async def factory(
        options: CommandOptions,
    ) -> AsyncIterator[CommandServices]:
        async with bootstrap.RuntimeBuilder(
            bootstrap.RuntimeSettings(config_dir=ROOT / "config")
        )(options) as services:
            monkeypatch.setattr(
                services.repository, "get_run", AsyncMock(return_value=None)
            )
            yield services

    result = CliRunner().invoke(cli.create_cli(factory), [command])
    assert result.exit_code == 1
    assert "requested run was not found" in result.stderr


def test_demo_refuses_to_fabricate_report_when_run_is_busy(
    monkeypatch: pytest.MonkeyPatch, http_receiver: Receiver
) -> None:

    @asynccontextmanager
    async def factory(
        options: CommandOptions,
    ) -> AsyncIterator[CommandServices]:
        async with bootstrap.RuntimeBuilder(
            bootstrap.RuntimeSettings(config_dir=ROOT / "config")
        )(options) as services:
            processor = services.processor()
            monkeypatch.setattr(
                processor,
                "execute",
                AsyncMock(return_value=RunReport(None, "busy")),
            )
            yield services

    result = CliRunner().invoke(cli.create_cli(factory), ["demo"])
    assert result.exit_code == 1
    assert "demo did not create a run" in result.stderr


def test_grace_expiry_is_intentional_clean_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "run_with_shutdown", AsyncMock(side_effect=GracefulStopError)
    )
    assert report(CliRunner().invoke(app(), ["run"])) == {"status": "stopped"}


def test_separate_run_dispatch_and_report_share_saved_storage(
    monkeypatch: pytest.MonkeyPatch,
    http_receiver: Receiver,
) -> None:
    repository = InMemoryRepository()
    monkeypatch.setattr(
        bootstrap,
        "_storage",
        lambda _stack, _settings, _options: bootstrap._Storage(
            repository, InMemoryRunLock(), True
        ),
    )
    application = app()
    saved = report(
        CliRunner().invoke(
            application,
            [
                "run",
                "--as-of",
                "2026-10-21",
                "--fixture",
                str(ROOT / "fixtures/tasks.json"),
            ],
        )
    )
    assert saved["storage"] == "postgresql"
    assert report(
        CliRunner().invoke(application, ["dispatch", "--limit", "2"])
    ) == {"processed": 2}
    stored = report(
        CliRunner().invoke(
            application, ["report", "--run-id", saved["run"]["id"]]
        )
    )
    assert stored["decisions"] == saved["decisions"]
    assert sum(action["status"] == "sent" for action in stored["actions"]) == 2
    assert report(CliRunner().invoke(application, ["reconcile"])) == {
        "processed": 0
    }


async def test_command_stop_preserves_pending_work_without_opening_new_clients(
    http_receiver: Receiver,
) -> None:
    async with bootstrap.RuntimeBuilder(
        bootstrap.RuntimeSettings(config_dir=ROOT / "config")
    )(CommandOptions(AS_OF, ROOT / "fixtures/tasks.json")) as services:
        await services.processor().execute(as_of=AS_OF)
        dispatcher = services.dispatcher()
        assert services.dispatcher() is dispatcher
        services.stop_claiming()
        assert await dispatcher.execute() == 0
        assert len(await services.repository.list_actions()) == 7


async def test_shutdown_before_lazy_dispatcher_creation_prevents_later_sends(
    http_receiver: Receiver,
) -> None:
    _, store, clients, requests = http_receiver
    async with bootstrap.RuntimeBuilder(
        bootstrap.RuntimeSettings(config_dir=ROOT / "config")
    )(CommandOptions(AS_OF, ROOT / "fixtures/tasks.json")) as services:
        await services.processor().execute(as_of=AS_OF)
        # SIGTERM during processing closes later delivery admission.
        services.stop_claiming()
        assert not clients
        assert await services.dispatcher().execute() == 0
        assert len(await services.repository.list_actions()) == 7
    assert not requests
    assert not store._entries
