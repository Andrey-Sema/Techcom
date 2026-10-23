"""Quiet decisions retain independent project authority."""

from api_support import NOW, Harness
from support import AS_OF, make_task, repo_policy
from techcom_followup.application.repository import DecisionWrite
from techcom_followup.domain.identity import state_fingerprint
from techcom_followup.domain.models import TaskStatus


async def test_quiet_run_is_visible_only_to_its_persisted_source_project() -> (
    None
):
    harness = Harness()
    task = make_task(project="A", status=TaskStatus.DONE)
    decision = repo_policy().decide(task, as_of=AS_OF)
    assert decision.target is None
    run = await harness.repo.start_run(AS_OF, "v", NOW)
    await harness.repo.record_decision(
        DecisionWrite(
            run.id,
            decision,
            state_fingerprint(task),
            source_project="A",
        ),
        NOW,
    )
    await harness.repo.finish_run(
        run.id,
        {
            "status": "completed",
            "decisions": 1,
            "rejected": 0,
        },
        NOW,
    )
    for project, expected in (("A", 200), ("B", 404)):
        async with harness.client(
            harness.token(
                sub="reader",
                projects=[project],
                scope="runs:read",
            )
        ) as client:
            assert (
                await client.get(f"/v1/runs/{run.id}")
            ).status_code == expected
