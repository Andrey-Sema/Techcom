"""Schedule-independent persistence, bounded admission and report order."""

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import override
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from persistence_support import NOW
from processing_support import Source
from support import AS_OF, repo_policy
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.repository import (
    DecisionWrite,
    StoredDecision,
)


class ScheduledRepository(InMemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.peak = 0
        self.finalized_while_active = False

    @override
    async def record_decision(
        self, write: DecisionWrite, now: datetime = NOW
    ) -> StoredDecision:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            for _ in range(1 + (write.source_position or 0) % 3):
                await asyncio.sleep(0)
            return await super().record_decision(write, now)
        finally:
            self.active -= 1

    @override
    async def finish_run(
        self, run_id: UUID, stats: Mapping[str, JsonValue], now: datetime = NOW
    ) -> bool:
        self.finalized_while_active |= self.active != 0
        return await super().finish_run(run_id, stats, now)


@settings(deadline=None, max_examples=25)
@given(width=st.integers(1, 8), count=st.integers(1, 35))
async def test_parallel_writes_preserve_order_and_idempotence(
    width: int,
    count: int,
) -> None:
    repository = ScheduledRepository()
    records: list[object] = list(
        {
            "task_id": f"TC-{index}",
            "project": "P",
            "task": "Work",
            "assigned_to": "One Owner",
            "status": "Blocked",
            "due_date": AS_OF.isoformat(),
            "last_update": AS_OF.isoformat(),
            "notes": "Waiting for materials",
        }
        for index in range(count)
    )
    processor = ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(records),
            repo_policy(),
            FixedClock(NOW),
            InMemoryRunLock(),
        ),
        write_concurrency=width,
    )
    first = await processor.execute(as_of=AS_OF)
    second = await processor.execute(as_of=AS_OF)
    assert first.status == second.status == "completed"
    assert 1 <= repository.peak <= width
    assert not repository.finalized_while_active
    assert repository.active == 0
    assert first.run_id is not None
    saved = await repository.list_decisions(first.run_id)
    assert [row.write.decision for row in saved] == list(first.decisions)
    assert [row.write.source_position for row in saved] == list(range(count))
    assert all(row.write.source_project == "P" for row in saved)
    assert second.enqueued == 0
    assert second.duplicates == first.enqueued


@given(width=st.one_of(st.integers(max_value=0), st.integers(min_value=17)))
def test_unbounded_concurrency_is_rejected(width: int) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        ProcessTasks(
            ProcessingDependencies(
                InMemoryRepository(),
                Source([]),
                repo_policy(),
                FixedClock(NOW),
                InMemoryRunLock(),
            ),
            write_concurrency=width,
        )


@pytest.mark.parametrize("cancel_child", [False, True])
async def test_failed_or_cancelled_writer_is_joined_before_run_finalization(
    cancel_child: bool,
) -> None:
    class FailedRepository(ScheduledRepository):
        @override
        async def record_decision(
            self, write: DecisionWrite, now: datetime = NOW
        ) -> StoredDecision:
            if write.source_position == 1:
                await asyncio.sleep(0)
                if cancel_child:
                    raise asyncio.CancelledError
                message = "injected write failure"
                raise RuntimeError(message)
            return await super().record_decision(write, now)

    repository = FailedRepository()
    records: list[object] = list(
        {
            "task_id": f"TC-{index}",
            "project": "P",
            "task": "Work",
            "assigned_to": "Owner",
            "status": "Blocked",
            "due_date": AS_OF.isoformat(),
            "last_update": AS_OF.isoformat(),
            "notes": "Waiting for materials",
        }
        for index in range(4)
    )
    processor = ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(records),
            repo_policy(),
            FixedClock(NOW),
            InMemoryRunLock(),
        ),
        write_concurrency=4,
    )
    with pytest.raises(
        asyncio.CancelledError if cancel_child else RuntimeError
    ):
        await processor.execute(as_of=AS_OF)
    assert repository.active == 0
    assert not repository.finalized_while_active
    assert all(
        run.stats and run.stats.get("status") == "failed"
        for run in repository._runs.values()
    )
