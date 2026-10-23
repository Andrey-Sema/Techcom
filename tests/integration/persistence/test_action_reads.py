from typing import Any

import pytest
from pydantic import TypeAdapter
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from action_read_support import enqueue_project
from techcom_followup.adapters.outbound import pg_repository as postgres
from techcom_followup.adapters.outbound.orm import decisions
from techcom_followup.application.repository import (
    ActionReadQuery,
    ActionRecord,
    DecisionWrite,
)

pytestmark = pytest.mark.integration


async def test_postgres_filters_before_row_materialization_and_limit(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = postgres.PgRepository(pg_engine)
    for index in range(6):
        await enqueue_project(repository, "foreign", f"TC-{index}")
    await enqueue_project(repository, "allowed", "TC-99")
    materialized: list[str | None] = []
    statements: list[str] = []
    bindings: list[object] = []
    decode = postgres._action

    def count_decode(row: Any) -> ActionRecord:
        action = decode(row)
        assert action.decision.target is not None
        materialized.append(action.decision.target.project)
        return action

    def query_executed(*arguments: Any) -> None:
        statements.append(str(arguments[2]))
        bindings.append(arguments[3])

    monkeypatch.setattr(postgres, "_action", count_decode)
    event.listen(pg_engine.sync_engine, "after_cursor_execute", query_executed)
    try:
        rows = await repository.query_actions(
            ActionReadQuery(
                projects=frozenset({"allowed"}),
                limit=1,
            )
        )
    finally:
        event.remove(
            pg_engine.sync_engine, "after_cursor_execute", query_executed
        )
    assert len(rows) == 1
    assert materialized == ["allowed"]
    assert len(statements) == 1
    assert statements[0].index("WHERE") < statements[0].index("LIMIT")
    assert "allowed" in repr(bindings)


async def test_postgres_hides_legacy_missing_target_before_decoding(
    pg_engine: AsyncEngine,
) -> None:
    repository = postgres.PgRepository(pg_engine)
    await enqueue_project(repository, "allowed", "TC-1")
    (stored,) = await repository.list_actions()
    (audit,) = await repository.list_audit()
    assert audit.run_id is not None
    (receipt,) = await repository.list_decisions(audit.run_id)
    data = TypeAdapter(DecisionWrite).dump_python(receipt.write, mode="json")
    data["decision"]["target"] = None
    async with pg_engine.begin() as connection:
        await connection.execute(
            decisions.update()
            .where(decisions.c.id == stored.decision_id)
            .values(data=data)
        )
    assert (
        await repository.query_actions(ActionReadQuery(all_projects=True))
        == ()
    )
