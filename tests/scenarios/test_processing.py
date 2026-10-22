import asyncio
from dataclasses import asdict, replace

import pytest
import stamina

from processing_support import fixture_records, stack
from support import AS_OF
from techcom_followup.application.processing_ports import DeliveryKind
from techcom_followup.application.repository import ActionStatus, ClaimRequest
from techcom_followup.domain.models import DecisionKind, ReasonCode
from techcom_followup.infrastructure.http_client import UpstreamError


async def test_s1_overdue_without_explanation_follows_up_owner() -> None:
    async with stack(fixture_records()[:1]) as app:
        report = await app.processor.execute(as_of=AS_OF)
        decision = report.decisions[0]
        assert decision.kind is DecisionKind.FOLLOW_UP_OWNER
        assert decision.target is not None
        assert decision.target.name == "John Smith"
        assert decision.rule_id == "R7"
        assert report.enqueued == 1
        action = (await app.repository.list_actions())[0]
        assert "3 day(s) overdue" in action.message.body
        assert await app.dispatcher.execute() == 1
        assert (await app.repository.list_actions())[
            0
        ].status is ActionStatus.SENT


async def test_s2_materials_block_requests_eta_not_completion() -> None:
    async with stack(fixture_records()[1:2]) as app:
        report = await app.processor.execute(as_of=AS_OF)
        decision = report.decisions[0]
        assert decision.kind is DecisionKind.REQUEST_ETA
        assert decision.target is not None
        assert "warehouse" in decision.target.name.casefold()
        body = (await app.repository.list_actions())[0].message.body
        assert "delivery date" in body
        assert "complete" not in body


async def test_s3_done_is_no_action() -> None:
    async with stack(fixture_records()[2:3]) as app:
        report = await app.processor.execute(as_of=AS_OF)
        assert report.decisions[0].kind is DecisionKind.NO_ACTION
        assert not await app.repository.list_actions()


@pytest.mark.parametrize(
    ("index", "reason", "target"),
    [
        (3, ReasonCode.MISSING_OWNER, "coordinator"),
        (4, ReasonCode.MISSING_DUE_DATE, "Alex Brown"),
    ],
    ids=["missing_owner", "missing_due_date"],
)
async def test_s3_missing_data_requests_clarification(
    index: int, reason: ReasonCode, target: str
) -> None:
    async with stack(fixture_records()[index : index + 1]) as app:
        report = await app.processor.execute(as_of=AS_OF)
        decision = report.decisions[0]
        assert decision.kind is DecisionKind.CLARIFY_MISSING_INFO
        assert decision.reason_code is reason
        assert decision.target is not None
        assert target.casefold() in decision.target.name.casefold()


async def test_s4_second_run_unchanged_data_sends_nothing() -> None:
    async with stack() as app:
        first = await app.processor.execute(as_of=AS_OF)
        assert await app.dispatcher.execute() == first.enqueued
        before = await app.repository.list_actions()
        second = await app.processor.execute(as_of=AS_OF)
        assert second.enqueued == 0
        assert second.duplicates == first.enqueued
        assert await app.dispatcher.execute() == 0
        assert await app.repository.list_actions() == before


async def test_s4_changed_due_date_creates_new_action() -> None:
    records = fixture_records()[:1]
    async with stack(records) as app:
        await app.processor.execute(as_of=AS_OF)
        assert isinstance(records[0], dict)
        records[0]["due_date"] = "2026-10-17"
        # A replacement source models a new upstream snapshot.
        async with stack(records) as changed:
            changed.processor._deps = replace(
                changed.processor._deps, repository=app.repository
            )
            report = await changed.processor.execute(as_of=AS_OF)
        assert report.enqueued == 1
        assert len(await app.repository.list_actions()) == 2


async def test_s5_rate_limit_and_transient_errors_retry_bounded() -> None:
    with stamina.set_testing(True, attempts=4, cap=True):
        async with stack([], faults="tasks:429@1,tasks:503@2") as app:
            report = await app.processor.execute(as_of=AS_OF)
            assert report.status == "completed"


async def test_fetch_exhausted_fails_cleanly() -> None:
    with stamina.set_testing(True, attempts=4, cap=True):
        async with stack(
            [], faults=",".join(f"tasks:503@{i}" for i in range(1, 5))
        ) as app:
            with pytest.raises(UpstreamError, match="source_transient"):
                await app.processor.execute(as_of=AS_OF)
            assert not await app.repository.list_actions()
            run = next(iter(app.repository._runs.values()))
            assert run.stats is not None
            assert run.stats["status"] == "failed"


@pytest.mark.parametrize("non_idempotent", [False, True])
async def test_s6_timeout_after_commit_does_not_duplicate(
    non_idempotent: bool,
) -> None:
    async with stack(
        fixture_records()[:1],
        faults="messages:timeout_after_commit@1",
        receiver_faults="messages:non_idempotent@1" if non_idempotent else "",
    ) as app:
        await app.processor.execute(as_of=AS_OF)
        assert await app.dispatcher.execute() == 1
        action = (await app.repository.list_actions())[0]
        assert action.status is ActionStatus.UNKNOWN
        assert await app.dispatcher.execute() == 0
        assert len(app.store._entries) == 1
        app.advance()
        assert await app.reconciler.execute() == 1
        assert (await app.repository.list_actions())[
            0
        ].status is ActionStatus.SENT
        assert await app.dispatcher.execute() == 0
        assert len(app.store._entries) == 1


async def test_s6_forced_repost_replays_same_acknowledgement() -> None:
    async with stack(fixture_records()[:1]) as app:
        await app.processor.execute(as_of=AS_OF)
        action = (await app.repository.list_actions())[0]
        first = await app.gateway.send(action)
        assert first.kind is DeliveryKind.ACCEPTED
        assert action.decision.target is not None
        response = await app.client.post(
            "https://mock-api/v1/messages",
            json={
                "task_id": action.decision.task_id,
                "target": asdict(action.decision.target),
                "message": {
                    "subject": action.message.subject,
                    "body": action.message.body,
                    "template_id": action.message.template_id,
                },
            },
            headers={"Idempotency-Key": str(action.identity.operation_id)},
        )
        assert response.headers["Idempotent-Replayed"] == "true"
        assert response.json()["provider_ref"] == first.provider_ref
        assert len(app.store._entries) == 1


async def test_timeout_before_commit_is_resent_once() -> None:
    async with stack(
        fixture_records()[:1], faults="messages:timeout_before_commit@1"
    ) as app:
        await app.processor.execute(as_of=AS_OF)
        await app.dispatcher.execute()
        app.advance()
        await app.reconciler.execute()
        assert (await app.repository.list_actions())[
            0
        ].status is ActionStatus.PENDING
        app.advance()
        await app.dispatcher.execute()
        assert (await app.repository.list_actions())[
            0
        ].status is ActionStatus.SENT
        assert len(app.store._entries) == 1


async def test_parallel_dispatchers_disjoint() -> None:
    async with stack() as app:
        report = await app.processor.execute(as_of=AS_OF)
        first, second = await asyncio.gather(
            app.dispatcher.execute(), app.dispatcher.execute()
        )
        assert first + second == report.enqueued
        assert len(app.store._entries) == report.enqueued


async def test_crash_after_send_reconciles_before_any_resend() -> None:

    async with stack(
        fixture_records()[:1], receiver_faults="messages:non_idempotent@1"
    ) as app:
        await app.processor.execute(as_of=AS_OF)
        action = (
            await app.repository.claim_actions(ClaimRequest(app.clock.now()))
        )[0]
        assert (await app.gateway.send(action)).kind is DeliveryKind.ACCEPTED
        # Simulate a crash before persisting the acknowledgement.
        app.advance()
        assert await app.dispatcher.execute() == 0
        assert (await app.repository.list_actions())[
            0
        ].status is ActionStatus.UNKNOWN
        assert await app.reconciler.execute() == 1
        assert (await app.repository.list_actions())[
            0
        ].status is ActionStatus.SENT
        assert len(app.store._entries) == 1
