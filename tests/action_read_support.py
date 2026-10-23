"""Stored project relationships for bounded-reader contract tests."""

from dataclasses import replace

from persistence_support import NOW, action_write
from techcom_followup.application.repository import Repository
from techcom_followup.domain.identity import ActionIdentity


async def enqueue_project(
    repository: Repository, project: str | None, task_id: str
) -> None:
    """Persist target authority independently of message wording."""
    write = await action_write(repository, task_id)
    target = write.decision.target
    assert target is not None
    decision = replace(write.decision, target=replace(target, project=project))
    await repository.record_decision(
        replace(
            write,
            decision=decision,
            identity=ActionIdentity.of(decision, write.fingerprint),
        ),
        NOW,
    )
