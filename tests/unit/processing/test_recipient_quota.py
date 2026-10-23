"""Rejected delivery admission cannot spend an attempt or reach a provider."""

from datetime import UTC, datetime

from persistence_support import NOW, action_write
from processing_support import Gateway
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.repository import ActionRecord, ActionStatus


class DeniedQuota:
    async def allow(self, _action: ActionRecord, _now: datetime) -> bool:
        return False


async def test_denied_daily_quota_defers_until_next_utc_day_without_send() -> (
    None
):
    repository = InMemoryRepository()
    await repository.record_decision(await action_write(repository), NOW)
    gateway = Gateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        quota=DeniedQuota(),
    )
    assert await dispatcher.execute(limit=1) == 0
    (action,) = await repository.list_actions()
    assert not gateway.sends
    assert action.status == ActionStatus.PENDING
    assert action.attempts == 0
    assert action.last_error == "recipient_limit"
    assert action.next_attempt_at == datetime(2026, 10, 22, tzinfo=UTC)


async def test_denied_recipient_does_not_starve_other_recipients() -> None:
    class SelectiveQuota:
        async def allow(self, action: ActionRecord, _now: datetime) -> bool:
            return action.decision.task_id == "TC-2"

    repository = InMemoryRepository()
    for task_id in ("TC-1", "TC-2"):
        await repository.record_decision(
            await action_write(repository, task_id), NOW
        )
    gateway = Gateway()
    dispatcher = DispatchActions(
        DeliveryDependencies(repository, gateway, FixedClock(NOW)),
        quota=SelectiveQuota(),
    )
    assert await dispatcher.execute(limit=2) == 1
    assert len(gateway.sends) == 1
    states = {
        action.decision.task_id: action
        for action in await repository.list_actions()
    }
    assert states["TC-1"].status == ActionStatus.PENDING
    assert states["TC-1"].attempts == 0
    assert states["TC-2"].status == ActionStatus.SENT
