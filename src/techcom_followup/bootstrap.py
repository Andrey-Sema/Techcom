"""Composition root: own resources and wire CLI use cases explicitly."""

import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Literal

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine

from techcom_followup.adapters.inbound.cli import create_cli
from techcom_followup.adapters.outbound.clock import FixedClock, SystemClock
from techcom_followup.adapters.outbound.delivery_http import (
    HttpDeliveryGateway,
)
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.llm.config import load_routing_config
from techcom_followup.adapters.outbound.llm.factory import (
    build_openai_client,
    build_router,
)
from techcom_followup.adapters.outbound.llm_stub import ScriptedLLM
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.adapters.outbound.run_lock import (
    InMemoryRunLock,
    PgRunLock,
)
from techcom_followup.adapters.outbound.task_source_http import HttpTaskSource
from techcom_followup.application.command_ports import (
    CommandOptions,
    CommandServices,
)
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.drafter import MessageDrafter
from techcom_followup.application.interpreter import NoteInterpreter
from techcom_followup.application.ports import LLMClient
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.processing_ports import (
    Clock,
    RunLock,
    TaskSource,
)
from techcom_followup.application.reconcile import ReconcileUnknown
from techcom_followup.application.repository import Repository
from techcom_followup.application.retry import RetryBudget
from techcom_followup.infrastructure.http_client import (
    HttpEndpoint,
    UpstreamError,
)
from techcom_followup.infrastructure.log_config import configure_logging
from techcom_followup.infrastructure.policy_files import (
    load_decision_policy,
)
from techcom_followup.infrastructure.resilience import CircuitBreaker
from techcom_followup.infrastructure.settings import load_openai_settings

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "mock-api"})
_MAX_FIXTURE_BYTES = 2_000_000
_MAX_FIXTURE_RECORDS = 10_000


class RuntimeSettings(BaseSettings):
    """Operator configuration with secret-safe validation failures."""

    model_config = SettingsConfigDict(
        env_prefix="FOLLOWUP_",
        extra="ignore",
        hide_input_in_errors=True,
    )
    database_url: SecretStr | None = None
    task_api_url: str = "http://127.0.0.1:8081"
    delivery_api_url: str = "http://127.0.0.1:8081"
    allowed_hosts: frozenset[str] = _LOCAL_HOSTS
    allow_local_http: bool = True
    config_dir: Path = Path("config")
    llm_provider: Literal["stub", "router"] = "stub"
    dispatch_enabled: bool = True


class FixtureTaskSource:
    """Read a local fixture through the TaskSource port."""

    def __init__(self, path: Path) -> None:
        """Keep the operator-selected path out of prompts and logs."""
        self._path = path

    async def fetch_tasks(self) -> tuple[object, ...]:
        """Reject oversized fixtures before normalization."""
        with self._path.open("rb") as stream:
            raw = stream.read(_MAX_FIXTURE_BYTES + 1)
        if len(raw) > _MAX_FIXTURE_BYTES:
            message = "fixture_size"
            raise UpstreamError(message)
        records: object = json.loads(raw)
        if (
            not isinstance(records, list)
            or len(records) > _MAX_FIXTURE_RECORDS
        ):
            message = "fixture_shape"
            raise UpstreamError(message)
        return tuple(records)


def _load_settings() -> RuntimeSettings:
    directory = Path("/run/secrets")
    return RuntimeSettings(
        _secrets_dir=directory if directory.is_dir() else None
    )


@dataclass(frozen=True, slots=True)
class _Storage:
    repository: Repository
    lock: RunLock
    persistent: bool


def _storage(
    stack: AsyncExitStack, settings: RuntimeSettings, options: CommandOptions
) -> _Storage:
    if settings.database_url is None or options.demo:
        return _Storage(
            InMemoryRepository(), InMemoryRunLock(), persistent=False
        )
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    stack.push_async_callback(engine.dispose)
    return _Storage(PgRepository(engine), PgRunLock(engine), persistent=True)


def _advance(clock: Clock, delta: timedelta) -> None:
    if isinstance(clock, FixedClock):
        clock.instant += delta


def _language(
    stack: AsyncExitStack,
    settings: RuntimeSettings,
    clock: Clock,
    *,
    disabled: bool = False,
) -> tuple[LLMClient | None, str]:
    if disabled:
        return None, "disabled"
    if settings.llm_provider == "stub":
        return ScriptedLLM(), "stub.v1"
    config = load_routing_config(settings.config_dir / "llm.toml")
    client = build_openai_client(load_openai_settings())
    stack.push_async_callback(client.close)
    router = build_router(config, client, today=clock.today)
    # Route, pricing and policy changes invalidate cache identity.

    return router, sha256(config.model_dump_json().encode()).hexdigest()


class _RuntimeScope:
    """Construct each dependency only when its command needs it."""

    def __init__(
        self,
        stack: AsyncExitStack,
        settings: RuntimeSettings,
        options: CommandOptions,
        clock: Clock,
        storage: _Storage,
    ) -> None:
        self._stack = stack
        self._settings = settings
        self._options = options
        self._clock = clock
        self._storage = storage
        self._client: httpx.AsyncClient | None = None
        self._processor: ProcessTasks | None = None
        self._dispatcher: DispatchActions | None = None

    def _endpoint(self, url: str) -> HttpEndpoint:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._stack.push_async_callback(self._client.aclose)
        return HttpEndpoint(
            self._client,
            url,
            allowed_hosts=self._settings.allowed_hosts,
            allow_local_http=self._settings.allow_local_http,
        )

    def processor(self) -> ProcessTasks:
        """Load policy and language settings only for processing."""
        if self._processor is None:
            settings, options = self._settings, self._options
            source: TaskSource = (
                FixtureTaskSource(options.fixture)
                if options.fixture is not None
                else HttpTaskSource(
                    self._endpoint(settings.task_api_url),
                    self._clock,
                    budget=RetryBudget(15),
                )
            )
            policy = load_decision_policy(settings.config_dir)
            language, model = _language(
                self._stack, settings, self._clock, disabled=options.dry_run
            )
            self._processor = ProcessTasks(
                ProcessingDependencies(
                    self._storage.repository,
                    source,
                    policy,
                    self._clock,
                    self._storage.lock,
                ),
                interpreter=NoteInterpreter(
                    language,
                    self._storage.repository,
                    model=model,
                    now=self._clock.now,
                    monotonic=time.monotonic,
                    due_soon_days=policy.due_soon_days,
                ),
                drafter=MessageDrafter(
                    None if settings.llm_provider == "stub" else language
                ),
            )
        return self._processor

    def _delivery(self) -> DeliveryDependencies:
        return DeliveryDependencies(
            self._storage.repository,
            HttpDeliveryGateway(
                self._endpoint(self._settings.delivery_api_url), self._clock
            ),
            self._clock,
        )

    def dispatcher(self) -> DispatchActions:
        """Share breaker state and the stop flag within the command."""
        if self._dispatcher is None:
            self._dispatcher = DispatchActions(
                self._delivery(),
                breaker=CircuitBreaker(
                    failure_threshold=5,
                    cooldown_s=30,
                    monotonic=time.monotonic,
                ),
                enabled=lambda: (
                    self._settings.dispatch_enabled
                    and os.environ.get(
                        "FOLLOWUP_DISPATCH_ENABLED", "true"
                    ).lower()
                    in {"true", "1", "yes"}
                ),
            )
        return self._dispatcher

    def reconciler(self) -> ReconcileUnknown:
        """Recover using delivery settings without LLM credentials."""
        return ReconcileUnknown(self._delivery())

    def stop_claiming(self) -> None:
        """Stop an existing dispatcher without opening a client."""
        if self._dispatcher is not None:
            self._dispatcher.stop()


class RuntimeBuilder:
    """Build a fresh runtime per CLI command without global clients."""

    def __init__(self, settings: RuntimeSettings | None = None) -> None:
        """Allow explicit settings in tests and managed deployments."""
        self._settings = settings

    @asynccontextmanager
    async def __call__(
        self, options: CommandOptions
    ) -> AsyncIterator[CommandServices]:
        """Close HTTP, LLM and database clients on every exit path."""
        settings = self._settings or _load_settings()
        if options.demo:
            settings = settings.model_copy(
                update={
                    "database_url": None,
                    "llm_provider": "stub",
                    "task_api_url": "http://127.0.0.1:8081",
                    "delivery_api_url": "http://127.0.0.1:8081",
                    "allowed_hosts": frozenset({"127.0.0.1"}),
                    "allow_local_http": True,
                }
            )
        clock: Clock = (
            FixedClock(
                datetime.combine(options.as_of, datetime.min.time(), UTC)
            )
            if options.demo
            else SystemClock()
        )
        async with AsyncExitStack() as stack:
            storage = _storage(stack, settings, options)
            scope = _RuntimeScope(stack, settings, options, clock, storage)
            yield CommandServices(
                storage.repository,
                scope.processor,
                scope.dispatcher,
                scope.reconciler,
                scope.stop_claiming,
                storage.persistent,
                partial(_advance, clock),
            )


def main() -> None:
    """Install redacted logging and invoke the Typer adapter."""
    configure_logging()
    create_cli(RuntimeBuilder())()
