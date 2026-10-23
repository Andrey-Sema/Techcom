"""Typer commands with injected runtimes and report exports."""

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

import structlog
import typer
from pydantic import JsonValue
from rich.console import Console

from techcom_followup.adapters.inbound.signals import (
    GracefulStopError,
    run_with_shutdown,
)
from techcom_followup.application.command_ports import (
    CommandOptions,
    CommandServices,
    RuntimeFactory,
)
from techcom_followup.application.reporting import proposal_view, run_view

_LOG = structlog.get_logger(__name__)
_ERRORS = {
    "persistent_storage_required": "This command requires PostgreSQL storage.",
    "run_missing": "The requested run was not found.",
    "demo_run_missing": "The demo did not create a run.",
}


class CommandError(Exception):
    """A closed operator error code, without dependency details."""


def _date(value: str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        message = "as_of must be an ISO date"
        raise typer.BadParameter(message) from error


def _write_output(output: Path, report: dict[str, JsonValue]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for filename, value in (
        ("run_report.json", report),
        ("decisions.json", report.get("decisions", [])),
    ):
        _write_json(output / filename, value)


def _write_json(path: Path, value: JsonValue) -> None:
    """Replace a report atomically with owner-only permissions."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(
                value, stream, ensure_ascii=True, sort_keys=True, indent=2
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class Commands:
    """Command adapter; state and side effects live in use cases."""

    def __init__(self, factory: RuntimeFactory) -> None:
        """Inject a new resource context for every invocation."""
        self._factory = factory
        self._console = Console(stderr=True, markup=False, highlight=False)

    def _execute(
        self,
        options: CommandOptions,
        command: Callable[[CommandServices], Awaitable[dict[str, JsonValue]]],
        output: Path | None,
    ) -> None:
        async def invoke() -> dict[str, JsonValue]:
            async with self._factory(options) as services:
                try:
                    return await run_with_shutdown(
                        lambda: command(services), services.stop_claiming
                    )
                except GracefulStopError:
                    return {"status": "stopped"}

        try:
            result = asyncio.run(invoke())
            if output is not None:
                _write_output(output, result)
        except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:
            _LOG.error("command.failed", error_type=type(error).__name__)
            self._console.print(
                _ERRORS.get(str(error), "Command failed.")
                if isinstance(error, CommandError)
                else "Command failed."
            )
            raise typer.Exit(1) from error
        _LOG.info("command.completed")
        typer.echo(json.dumps(result, ensure_ascii=True, sort_keys=True))

    def run(
        self,
        *,
        as_of: Annotated[
            str | None, typer.Option(envvar="FOLLOWUP_AS_OF")
        ] = None,
        fixture: Annotated[Path | None, typer.Option()] = None,
        dispatch: Annotated[bool, typer.Option("--dispatch")] = False,
        dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
        output: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Compute decisions and optionally dispatch the outbox."""
        options = CommandOptions(_date(as_of), fixture, dry_run=dry_run)

        async def command(services: CommandServices) -> dict[str, JsonValue]:
            report = await services.processor().execute(
                as_of=options.as_of, dry_run=dry_run
            )
            if dispatch and not dry_run:
                await services.dispatcher().execute()
            if report.run_id is None:
                return {
                    "status": report.status,
                    "decisions": [
                        proposal_view(plan) for plan in report.plans
                    ],
                }
            stored = await services.repository.get_run(report.run_id)
            if stored is None:
                message = "run_missing"
                raise CommandError(message)
            result = await run_view(services.repository, stored)
            result["storage"] = (
                "postgresql" if services.persistent else "memory"
            )
            return result

        self._execute(options, command, output)

    def dispatch(
        self,
        limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
    ) -> None:
        """Dispatch saved work; separate commands require PostgreSQL."""

        async def command(services: CommandServices) -> dict[str, JsonValue]:
            _require_persistent(services)
            return {
                "processed": await services.dispatcher().execute(limit=limit)
            }

        self._execute(CommandOptions(_date(None)), command, None)

    def reconcile(
        self,
        limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
    ) -> None:
        """Look up unknown operations before authorizing any resend."""

        async def command(services: CommandServices) -> dict[str, JsonValue]:
            _require_persistent(services)
            return {
                "processed": await services.reconciler().execute(limit=limit)
            }

        self._execute(CommandOptions(_date(None)), command, None)

    def report(self, run_id: Annotated[UUID, typer.Option()]) -> None:
        """Print the saved report for one completed or failed run."""

        async def command(services: CommandServices) -> dict[str, JsonValue]:
            _require_persistent(services)
            stored = await services.repository.get_run(run_id)
            if stored is None:
                message = "run_missing"
                raise CommandError(message)
            return await run_view(services.repository, stored)

        self._execute(CommandOptions(_date(None)), command, None)

    def demo(
        self,
        as_of: Annotated[str, typer.Option()] = "2026-10-21",
        output: Annotated[Path | None, typer.Option()] = None,
    ) -> None:
        """Run, dispatch, reconcile and rerun against the local mock."""
        options = CommandOptions(_date(as_of), demo=True)

        async def command(services: CommandServices) -> dict[str, JsonValue]:
            first = await services.processor().execute(as_of=options.as_of)
            delivered = await services.dispatcher().execute()
            services.advance_demo_clock(timedelta(minutes=6))
            reconciled = await services.reconciler().execute()
            second = await services.processor().execute(as_of=options.as_of)
            if first.run_id is None:
                message = "demo_run_missing"
                raise CommandError(message)
            stored = await services.repository.get_run(first.run_id)
            if stored is None:
                message = "run_missing"
                raise CommandError(message)
            result = await run_view(services.repository, stored)
            result["demo"] = {
                "first_enqueued": first.enqueued,
                "rerun_duplicates": second.duplicates,
                "dispatch_processed": delivered,
                "reconciled": reconciled,
            }
            return result

        self._execute(options, command, output)

    def prune(self) -> None:
        """Apply data retention (docs/18) with the maintenance role."""

        async def command(services: CommandServices) -> dict[str, JsonValue]:
            _require_persistent(services)
            total, complete = await services.pruner().execute()
            return {
                "deleted": {
                    "llm_cache": total.cache,
                    "actions": total.actions,
                    "decisions": total.decisions,
                    "audit_events": total.audit,
                    "runs": total.runs,
                    "admission_history": total.admissions,
                },
                "complete": complete,
            }

        self._execute(CommandOptions(_date(None)), command, None)


def _require_persistent(services: CommandServices) -> None:
    if not services.persistent:
        message = "persistent_storage_required"
        raise CommandError(message)


def create_cli(factory: RuntimeFactory) -> typer.Typer:
    """Build a command group without importing its composition root."""
    commands = Commands(factory)
    app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
    for name in ("run", "dispatch", "reconcile", "report", "demo"):
        app.command(name=name)(getattr(commands, name))
    maintenance = typer.Typer(
        no_args_is_help=True,
        help="Data retention; run with the maintenance database role.",
    )
    maintenance.command(name="prune")(commands.prune)
    app.add_typer(maintenance, name="maintenance")
    return app
