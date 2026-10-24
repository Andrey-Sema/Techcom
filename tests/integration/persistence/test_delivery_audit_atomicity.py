import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from persistence_support import NOW, action_write
from processing_support import Gateway
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.processing_ports import (
    DeliveryKind,
    DeliveryResult,
)
from techcom_followup.application.reconcile import ReconcileUnknown
from techcom_followup.application.repository import ActionStatus

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stage", ["dispatch", "reconcile"])
async def test_failed_receipt_rolls_back_result_and_reconciles_without_resend(
    pg_engine: AsyncEngine, stage: str
) -> None:
    repository = PgRepository(pg_engine)
    await repository.record_decision(await action_write(repository), NOW)
    gateway = Gateway(
        lookup_result=DeliveryResult(DeliveryKind.ACCEPTED, "ref")
    )
    dependencies = DeliveryDependencies(repository, gateway, FixedClock(NOW))
    dispatcher = DispatchActions(dependencies)
    reconciler = ReconcileUnknown(dependencies)
    if stage == "reconcile":
        gateway.result = DeliveryResult(DeliveryKind.UNKNOWN)
        await dispatcher.execute()
        # UNKNOWN is scheduled with backoff; make it due for this cycle.
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("UPDATE actions SET next_attempt_at=:now"), {"now": NOW}
            )
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("""
        CREATE FUNCTION test_fail_receipt() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind IN ('delivery.sent','delivery.reconciled') THEN
                RAISE EXCEPTION 'injected receipt failure';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER test_fail_receipt BEFORE INSERT ON audit_events
            FOR EACH ROW EXECUTE FUNCTION test_fail_receipt();
        """)
        )
    try:
        with pytest.raises(DBAPIError, match="injected receipt failure"):
            await (dispatcher if stage == "dispatch" else reconciler).execute()
        (uncertain,) = await repository.list_actions()
        assert uncertain.status is ActionStatus.UNKNOWN
        assert not any(
            event.kind in {"delivery.sent", "delivery.reconciled"}
            for event in await repository.list_audit()
        )
    finally:
        async with pg_engine.begin() as connection:
            await connection.execute(
                text(
                    "DROP TRIGGER test_fail_receipt ON audit_events; "
                    "DROP FUNCTION test_fail_receipt()"
                )
            )
    assert await reconciler.execute() == 1
    assert (await repository.list_actions())[0].status is ActionStatus.SENT
    assert len(gateway.sends) == 1
    assert (
        len(
            [
                event
                for event in await repository.list_audit()
                if event.kind == "delivery.reconciled"
            ]
        )
        == 1
    )
