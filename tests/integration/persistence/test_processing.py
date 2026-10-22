from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from processing_support import fixture_records, stack
from support import AS_OF
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.process_tasks import ProcessTasks
from techcom_followup.application.reconcile import ReconcileUnknown
from techcom_followup.application.repository import ActionStatus

pytestmark = pytest.mark.integration


async def test_pg_pipeline_reconciles_non_idempotent_lost_ack(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    async with stack(
        fixture_records()[:1],
        faults="messages:timeout_after_commit@1",
        receiver_faults="messages:non_idempotent@1",
    ) as app:
        processor = ProcessTasks(
            replace(app.processor._deps, repository=repository)
        )
        deps = DeliveryDependencies(repository, app.gateway, app.clock)
        dispatcher = DispatchActions(deps)
        first = await processor.execute(as_of=AS_OF)
        assert first.enqueued == 1
        assert await dispatcher.execute() == 1
        assert (await repository.list_actions())[
            0
        ].status is ActionStatus.UNKNOWN
        app.advance()
        assert await dispatcher.execute() == 0
        assert await ReconcileUnknown(deps).execute() == 1
        assert (await repository.list_actions())[0].status is ActionStatus.SENT
        assert len(app.store._entries) == 1
        assert (await processor.execute(as_of=AS_OF)).duplicates == 1
        assert await dispatcher.execute() == 0
