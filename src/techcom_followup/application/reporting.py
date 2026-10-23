"""Explicit report projections; never expose raw repository objects."""

from pydantic import JsonValue, TypeAdapter

from techcom_followup.application.repository import (
    ActionRecord,
    DecisionWrite,
    Repository,
    RunRecord,
    StoredDecision,
)

_RUN = TypeAdapter(RunRecord)
_ACTION = TypeAdapter(ActionRecord)


def decision_view(stored: StoredDecision) -> dict[str, JsonValue]:
    """Flatten a saved decision into published business fields."""
    return _write_view(stored.write, stored.outcome.value)


def proposal_view(write: DecisionWrite) -> dict[str, JsonValue]:
    """Expose prepared facts and messages without fabricated row IDs."""
    return _write_view(write, "planned")


def _write_view(write: DecisionWrite, outcome: str) -> dict[str, JsonValue]:
    decision = write.decision
    target = decision.target
    message = write.message
    identity = write.identity
    flags: list[JsonValue] = [flag.value for flag in sorted(decision.flags)]
    return {
        "task_id": decision.task_id,
        "decision": decision.kind.value,
        "target": None
        if target is None
        else {
            "kind": target.kind.value,
            "name": target.name,
            "project": target.project,
        },
        "rule_id": decision.rule_id,
        "reason_code": decision.reason_code.value,
        "reason": decision.reason,
        "flags": flags,
        "message": None
        if message is None
        else {
            "subject": message.subject,
            "body": message.body,
            "template_id": message.template_id,
            "source": message.source.value,
        },
        "idempotency_key": None
        if identity is None
        else identity.idempotency_key,
        "operation_id": None
        if identity is None
        else str(identity.operation_id),
        "outcome": outcome,
    }


async def run_view(
    repository: Repository, run: RunRecord
) -> dict[str, JsonValue]:
    """Project a run plus its decisions and delivery status counts."""
    decisions = await repository.list_decisions(run.id)
    actions = await repository.list_run_actions(run.id)
    return {
        "run": _RUN.dump_python(run, mode="json"),
        "decisions": [decision_view(item) for item in decisions],
        "actions": [
            _ACTION.dump_python(
                item, mode="json", exclude={"terminal_at", "source_project"}
            )
            for item in actions
        ],
    }
