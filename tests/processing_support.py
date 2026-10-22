"""Typed workflow test doubles and a real hermetic HTTP stack."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from persistence_support import NOW
from support import ROOT, repo_policy
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.delivery_http import (
    HttpDeliveryGateway,
)
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.adapters.outbound.task_source_http import HttpTaskSource
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.processing_ports import (
    DeliveryKind,
    DeliveryResult,
)
from techcom_followup.application.reconcile import ReconcileUnknown
from techcom_followup.application.repository import ActionRecord
from techcom_followup.application.retry import RetryBudget
from techcom_followup.infrastructure.http_client import HttpEndpoint
from techcom_mocks.app import create_app
from techcom_mocks.faults import FaultInjectingTransport, FaultPlan
from techcom_mocks.store import DeliveryStore


@dataclass
class Source:
    records: list[object] = field(default_factory=list)

    async def fetch_tasks(self) -> list[object]:
        return self.records


@dataclass
class Gateway:
    result: DeliveryResult = field(
        default_factory=lambda: DeliveryResult(DeliveryKind.ACCEPTED, "ref")
    )
    lookup_result: DeliveryResult = field(
        default_factory=lambda: DeliveryResult(DeliveryKind.ABSENT)
    )
    sends: list[UUID] = field(default_factory=list)
    lookups: list[UUID] = field(default_factory=list)

    async def send(self, action: ActionRecord) -> DeliveryResult:
        self.sends.append(action.identity.operation_id)
        return self.result

    async def lookup(self, operation_id: UUID) -> DeliveryResult:
        self.lookups.append(operation_id)
        return self.lookup_result


def fixture_records() -> list[object]:
    return TypeAdapter(list[object]).validate_json(
        (ROOT / "fixtures/tasks.json").read_bytes()
    )


@dataclass
class Stack:
    repository: InMemoryRepository
    clock: FixedClock
    processor: ProcessTasks
    dispatcher: DispatchActions
    reconciler: ReconcileUnknown
    gateway: HttpDeliveryGateway
    store: DeliveryStore
    client: httpx.AsyncClient

    def advance(self, seconds: int = 600) -> None:
        self.clock.instant += timedelta(seconds=seconds)


@asynccontextmanager
async def stack(
    records: list[object] | None = None,
    *,
    faults: str = "",
    receiver_faults: str = "",
) -> AsyncIterator[Stack]:
    clock = FixedClock(NOW)
    repository = InMemoryRepository()
    store = DeliveryStore()
    app = create_app(
        tasks=fixture_records() if records is None else records,
        store=store,
        faults=FaultPlan.parse(receiver_faults),
    )
    transport = FaultInjectingTransport(
        httpx.ASGITransport(app=app), FaultPlan.parse(faults)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        endpoint = HttpEndpoint(
            client, "https://mock-api", allowed_hosts=frozenset({"mock-api"})
        )
        gateway = HttpDeliveryGateway(endpoint, clock)
        deps = DeliveryDependencies(repository, gateway, clock)
        yield Stack(
            repository,
            clock,
            ProcessTasks(
                ProcessingDependencies(
                    repository,
                    HttpTaskSource(endpoint, clock, budget=RetryBudget(20)),
                    repo_policy(),
                    clock,
                    InMemoryRunLock(),
                )
            ),
            DispatchActions(deps, jitter=lambda: 1),
            ReconcileUnknown(deps),
            gateway,
            store,
            client,
        )
