"""Disposable DAST target: real API/workflow, synthetic data, no provider."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from techcom_followup.adapters.inbound.api.app import create_app
from techcom_followup.adapters.inbound.api.middleware import ApiSettings
from techcom_followup.adapters.inbound.api.security import (
    TokenSettings,
    TokenVerifier,
)
from techcom_followup.adapters.outbound.clock import SystemClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.infrastructure.policy_files import load_decision_policy
from techcom_followup.serving import ApiClock


class Source:
    async def fetch_tasks(self) -> tuple[object, ...]:
        return tuple(json.loads(Path("/audit/tasks.json").read_bytes()))


def create_target() -> FastAPI:
    """No database credentials, signing key, live task source or delivery I/O."""
    repository = InMemoryRepository()
    clock = ApiClock()
    processor = ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(),
            load_decision_policy(Path("config")),
            clock,
            InMemoryRunLock(),
        )
    )
    app = create_app(
        repository=repository,
        runner=processor,
        clock=clock,
        settings=ApiSettings(rate_capacity=1_000_000),
        verifier=TokenVerifier(
            TokenSettings(
                "dast.local",
                "techcom.audit",
                {"audit": Path("/audit/public.pem").read_bytes()},
                frozenset({"zap"}),
            )
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await processor.execute(as_of=SystemClock().today())
        yield

    app.router.lifespan_context = lifespan
    return app
