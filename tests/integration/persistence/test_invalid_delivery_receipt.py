import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from persistence_support import NOW, action_write
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.delivery_http import (
    HttpDeliveryGateway,
)
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.repository import ActionStatus
from techcom_followup.infrastructure.http_client import HttpEndpoint

pytestmark = pytest.mark.integration


async def test_invalid_reference_never_reaches_postgresql_text(
    pg_engine: AsyncEngine,
) -> None:
    repository = PgRepository(pg_engine)
    write = await action_write(repository)
    await repository.record_decision(write, NOW)
    healthy = await action_write(repository, "TC-2")
    await repository.record_decision(healthy, NOW)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "operation_id": request.headers["Idempotency-Key"],
                "status": "sent",
                "provider_ref": "invalid\x00reference"
                if json.loads(request.content)["task_id"]
                == write.decision.task_id
                else "valid-reference",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        endpoint = HttpEndpoint(
            client, "https://mock-api", allowed_hosts=frozenset({"mock-api"})
        )
        clock = FixedClock(NOW)
        dispatcher = DispatchActions(
            DeliveryDependencies(
                repository,
                HttpDeliveryGateway(endpoint, clock),
                clock,
            )
        )
        assert await dispatcher.execute() == 2
    actions = {
        action.decision.task_id: action
        for action in await repository.list_actions()
    }
    action = actions[write.decision.task_id]
    assert action.status is ActionStatus.UNKNOWN
    assert action.provider_ref is None
    assert action.last_error == "invalid_receipt"
    assert actions[healthy.decision.task_id].status is ActionStatus.SENT
    events = [
        event
        for event in await repository.list_audit()
        if event.task_id == write.decision.task_id
    ]
    assert events[-1].kind == "delivery.unknown"
