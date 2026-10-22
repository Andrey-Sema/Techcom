"""Delivery audit metadata without message bodies or note evidence."""

from datetime import datetime

from techcom_followup.application.repository import (
    ActionRecord,
    ActionStatus,
    AuditEvent,
    Repository,
)


async def audit_delivery(
    repository: Repository,
    action: ActionRecord,
    now: datetime,
    kind: str,
    status: ActionStatus,
) -> None:
    """Append code-owned identity, attempt and durable status facts."""
    await repository.append_audit(
        AuditEvent(
            kind,
            now,
            {
                "action_id": str(action.id),
                "operation_id": str(action.identity.operation_id),
                "attempt": action.attempts,
                "status": status.value,
            },
            task_id=action.decision.task_id,
        )
    )
