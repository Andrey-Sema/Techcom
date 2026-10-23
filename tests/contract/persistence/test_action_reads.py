from dataclasses import replace
from typing import Any

import pytest

from action_read_support import enqueue_project
from techcom_followup.application.repository import (
    ActionReadQuery,
    ActionRecord,
    ActionStatus,
    Repository,
)


async def seeded(repository: Repository) -> tuple[ActionRecord, ...]:
    for index, project in enumerate(("A", "B", "a", None)):
        await enqueue_project(repository, project, f"TC-{index}")
    return tuple(
        sorted(await repository.list_actions(), key=lambda row: row.id)
    )


async def test_authority_is_filtered_before_the_limit(
    repository: Repository,
) -> None:
    rows = await seeded(repository)
    # Every stored project differs, so earlier rows cannot consume the
    # authorized limit, regardless of backend UUID generation.
    candidates = [
        row
        for row in rows
        if row.decision.target is not None
        and row.decision.target.project is not None
    ]
    last = candidates[-1]
    target = last.decision.target
    assert target is not None
    project = target.project
    assert project is not None
    query = ActionReadQuery(projects=frozenset({project}), limit=1)
    assert await repository.query_actions(query) == (last,)
    assert await repository.query_actions(ActionReadQuery()) == ()
    global_rows = await repository.query_actions(
        ActionReadQuery(all_projects=True, limit=2)
    )
    assert global_rows == rows[:2]


async def test_client_filters_only_narrow_authorized_projects(
    repository: Repository,
) -> None:
    await seeded(repository)
    query = ActionReadQuery(projects=frozenset({"A"}))
    (own,) = await repository.query_actions(query)
    assert own.decision.target is not None
    assert own.decision.target.project == "A"
    for filter_query in (
        replace(query, project="A"),
        replace(query, task_id=own.decision.task_id),
        replace(query, status=ActionStatus.PENDING),
    ):
        assert await repository.query_actions(filter_query) == (own,)
    for filter_query in (
        replace(query, project="B"),
        replace(query, project="A' OR true --"),
        replace(query, task_id="TC-1"),
        replace(query, status=ActionStatus.SENT),
        replace(query, projects=frozenset({"unknown"})),
        replace(query, projects=frozenset({"*"})),
    ):
        assert await repository.query_actions(filter_query) == ()
    assert (
        await repository.query_actions(
            ActionReadQuery(all_projects=True, project="a")
        )
        != ()
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"all_projects": "false"},
        {"projects": {"A"}},
        {"projects": frozenset({1})},
        {"projects": frozenset({""})},
        {"projects": frozenset({"x" * 201})},
        {"projects": frozenset(str(index) for index in range(129))},
        {"all_projects": True, "projects": frozenset({"A"})},
    ],
)
async def test_repository_rejects_unbounded_or_ambiguous_query(
    repository: Repository, changes: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="action read"):
        await repository.query_actions(ActionReadQuery(**changes))


@pytest.mark.parametrize("value", ["A\0", "\ud800"])
@pytest.mark.parametrize("field", ["project", "task_id", "projects"])
async def test_query_rejects_unrepresentable_database_text(
    repository: Repository, value: str, field: str
) -> None:
    options: dict[str, Any] = {
        field: frozenset({value}) if field == "projects" else value
    }
    with pytest.raises(ValueError, match="UTF-8"):
        await repository.query_actions(ActionReadQuery(**options))
