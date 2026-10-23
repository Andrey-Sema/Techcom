"""Own the public API, private metrics listener and OTLP log lifecycle."""

import asyncio
import json
import signal
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Self, override
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from techcom_followup.adapters.inbound.api.app import (
    create_app,
    create_metrics_app,
)
from techcom_followup.adapters.inbound.api.middleware import ApiSettings
from techcom_followup.adapters.inbound.api.observability import Observer
from techcom_followup.adapters.inbound.api.security import (
    TokenSettings,
    TokenVerifier,
)
from techcom_followup.adapters.outbound.admission import PgRequestLimiter
from techcom_followup.adapters.outbound.clock import SystemClock
from techcom_followup.application.command_ports import CommandOptions
from techcom_followup.application.process_tasks import ProcessTasks, RunReport
from techcom_followup.bootstrap import RuntimeBuilder, RuntimeSettings
from techcom_followup.infrastructure.log_config import configure_logging
from techcom_followup.infrastructure.otlp import OtlpLogExporter

MAX_PUBLIC_KEY_BYTES = 65_536
MAX_SIGNING_KEYS = 16


class ApiRuntimeSettings(BaseSettings):
    """No default issuer keys, wildcard subjects or external collectors."""

    model_config = SettingsConfigDict(
        env_prefix="FOLLOWUP_API_",
        extra="ignore",
        hide_input_in_errors=True,
    )
    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    public_keys_file: Path
    all_project_subjects: frozenset[str] = frozenset()
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1024, le=65535)
    metrics_host: str = "127.0.0.1"
    metrics_port: int = Field(default=9464, ge=1024, le=65535)
    otlp_endpoint: str = Field(
        default="https://alloy:4318/v1/logs", max_length=2048
    )
    collector_hosts: frozenset[str] = frozenset({"alloy"})
    allow_internal_http: bool = False
    rate_capacity: int = Field(default=60, ge=1, le=1_000_000)
    rate_window_s: float = Field(
        default=60, gt=0, le=86400, allow_inf_nan=False
    )
    max_rate_subjects: int = Field(default=1024, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_listeners(self) -> Self:
        """Reject ambiguous listeners and unapproved collector destinations."""
        parsed = urlsplit(self.otlp_endpoint)
        # Access validates a malformed or out-of-range port before export.
        _port = parsed.port
        checks = (
            self.port != self.metrics_port,
            parsed.hostname in self.collector_hosts,
            parsed.username is None,
            parsed.password is None,
            not parsed.query,
            not parsed.fragment,
            parsed.path == "/v1/logs",
            parsed.scheme == "https"
            or (self.allow_internal_http and parsed.scheme == "http"),
        )
        if not all(checks):
            message = "invalid listener or collector configuration"
            raise ValueError(message)
        return self


class ApiClock(SystemClock):
    """Security time combines UTC claims with monotonic local durations."""

    @staticmethod
    def monotonic() -> float:
        """Read process-local duration independently of wall-clock time."""
        return time.monotonic()


def token_verifier(settings: ApiRuntimeSettings) -> TokenVerifier:
    """Load a bounded map of public PEM keys, never private signing keys."""
    with settings.public_keys_file.open("rb") as stream:
        raw = stream.read(MAX_PUBLIC_KEY_BYTES + 1)
    if len(raw) > MAX_PUBLIC_KEY_BYTES:
        message = "public-key configuration is too large"
        raise ValueError(message)
    keys = json.loads(raw)
    if (
        not isinstance(keys, dict)
        or not 1 <= len(keys) <= MAX_SIGNING_KEYS
        or any(
            not isinstance(key, str) or not isinstance(pem, str)
            for key, pem in keys.items()
        )
    ):
        message = "invalid public-key map"
        raise ValueError(message)
    return TokenVerifier(
        TokenSettings(
            settings.issuer,
            settings.audience,
            {key: pem.encode("ascii") for key, pem in keys.items()},
            settings.all_project_subjects,
        )
    )


class _Listener(uvicorn.Server):
    """A listener whose signals are owned by the shared runtime."""

    @override
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Both listeners share the composition root's signal lifecycle."""
        yield


@dataclass(frozen=True, slots=True)
class _Runs:
    """Keep read endpoints independent of provider credential setup."""

    factory: Callable[[], ProcessTasks]

    async def execute(
        self, *, as_of: date, dry_run: bool = False
    ) -> RunReport:
        """Construct processing only after authenticated trigger admission."""
        return await self.factory().execute(as_of=as_of, dry_run=dry_run)


async def _check_storage(engine: AsyncEngine) -> None:
    """Reject a missing API migration or admission grant before binding ports."""
    async with engine.connect() as connection:
        await connection.execute(
            text(
                "SELECT d.source_project,d.source_position,a.terminal_at "
                "FROM public.decisions d CROSS JOIN public.actions a LIMIT 0"
            )
        )
        allowed = await connection.execute(
            text(
                "SELECT has_function_privilege(current_user, "
                "'public.admit_api_request(text,text,integer,"
                "double precision,integer)', 'EXECUTE') "
                "AND NOT has_table_privilege("
                "current_user,'public.action_keys',"
                "'INSERT,UPDATE,DELETE,TRUNCATE') "
                "AND NOT has_table_privilege("
                "current_user,'public.audit_events',"
                "'UPDATE,DELETE,TRUNCATE') "
                "AND NOT has_schema_privilege(current_user,'public','CREATE') "
                "AND NOT EXISTS (SELECT FROM pg_catalog.pg_roles "
                "WHERE rolname IN (current_user, session_user) AND "
                "(rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls))"
            )
        )
        if not allowed.scalar_one():
            message = "API admission permission or role boundary is invalid"
            raise ValueError(message)


@asynccontextmanager
async def _logs(
    api: ApiRuntimeSettings, observer: Observer
) -> AsyncIterator[None]:
    async with httpx.AsyncClient(
        timeout=2,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=1),
    ) as client:
        exporter = OtlpLogExporter(
            client, api.otlp_endpoint, observer.registry
        )
        configure_logging(exporter.capture)
        worker = asyncio.create_task(exporter.run())
        try:
            yield
        finally:
            try:
                await exporter.close()
            finally:
                worker.cancel()
                try:
                    with suppress(asyncio.CancelledError):
                        await worker
                finally:
                    configure_logging()


async def _run_listeners(
    listeners: list[_Listener], stop_claiming: Callable[[], None]
) -> None:
    loop = asyncio.get_running_loop()

    def stop() -> None:
        stop_claiming()
        for listener in listeners:
            listener.should_exit = True

    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stop)
    tasks = [asyncio.create_task(listener.serve()) for listener in listeners]
    try:
        completed, _pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in completed:
            task.result()
    finally:
        stop()
        try:
            async with asyncio.timeout(25):
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for sig in signals:
                loop.remove_signal_handler(sig)


def _listeners(
    app: FastAPI, observer: Observer, api: ApiRuntimeSettings
) -> list[_Listener]:
    return [
        _Listener(
            uvicorn.Config(
                app,
                host=api.host,
                port=api.port,
                access_log=False,
                proxy_headers=False,
                timeout_graceful_shutdown=15,
                limit_concurrency=128,
            )
        ),
        _Listener(
            uvicorn.Config(
                create_metrics_app(observer),
                host=api.metrics_host,
                port=api.metrics_port,
                access_log=False,
                proxy_headers=False,
                timeout_graceful_shutdown=5,
                limit_concurrency=16,
            )
        ),
    ]


async def serve(runtime: RuntimeSettings, api: ApiRuntimeSettings) -> None:
    """Share authority/admission while keeping metrics on a private socket."""
    if runtime.database_url is None:
        message = "the API requires persistent PostgreSQL storage"
        raise ValueError(message)
    verifier = token_verifier(api)
    engine = create_async_engine(
        runtime.database_url.get_secret_value(),
        pool_size=2,
        max_overflow=0,
        pool_timeout=5,
        pool_pre_ping=True,
        isolation_level="READ COMMITTED",
        connect_args={
            "options": "-c statement_timeout=5000 -c lock_timeout=2000"
        },
    )
    clock = ApiClock()
    try:
        await _check_storage(engine)
        async with RuntimeBuilder(runtime)(
            CommandOptions(clock.today())
        ) as services:
            observer = Observer()
            async with _logs(api, observer):
                settings = ApiSettings(
                    rate_capacity=api.rate_capacity,
                    rate_refill_s=api.rate_window_s,
                    max_rate_subjects=api.max_rate_subjects,
                )
                app = create_app(
                    repository=services.repository,
                    runner=_Runs(services.processor),
                    verifier=verifier,
                    clock=clock,
                    settings=settings,
                    observer=observer,
                    limiter=PgRequestLimiter(
                        engine,
                        capacity=api.rate_capacity,
                        period_s=api.rate_window_s,
                        max_subjects=api.max_rate_subjects,
                    ),
                )
                await _run_listeners(
                    _listeners(app, observer, api), services.stop_claiming
                )
    finally:
        await engine.dispose()


def main() -> None:
    """Run one API process; scale processes using the shared database gates."""
    configure_logging()
    secrets = Path("/run/secrets")
    directory = secrets if secrets.is_dir() else None
    try:
        asyncio.run(
            serve(
                RuntimeSettings(_secrets_dir=directory),
                ApiRuntimeSettings(_secrets_dir=directory),
            )
        )
    except Exception:
        message = "API startup or runtime failed"
        raise SystemExit(message) from None
