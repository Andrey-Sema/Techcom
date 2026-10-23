"""Reviewed agent wording; model prose never reaches delivery."""

from techcom_followup.domain.messages import MessageTemplate
from techcom_followup.domain.models import DecisionKind

_WORDING = {
    DecisionKind.FOLLOW_UP_OWNER: (
        "Quick check-in on {task_id}: {task_title}",
        (
            "Hi {recipient_name}, I am checking in on {task_id} "
            "'{task_title}' ({project}), due {due_date}. Our tracker shows: "
            "{reason_text} Could you share where it stands, anything "
            "blocking it and a realistic completion date? Thank you!"
        ),
    ),
    DecisionKind.REQUEST_ETA: (
        "Delivery ETA needed for {task_id}: {task_title}",
        (
            "Hello {recipient_name}, {task_id} '{task_title}' ({project}) is "
            "waiting on materials and is due {due_date}. Could you confirm "
            "when the delivery will arrive, or tell us who can?"
        ),
    ),
    DecisionKind.CLARIFY_MISSING_INFO: (
        "Details needed for {task_id}: {task_title}",
        (
            "Hello {recipient_name}, {task_id} '{task_title}' ({project}) "
            "needs clarification. Our tracker shows: {reason_text} "
            "Could you clarify the details or tell us who can?"
        ),
    ),
    DecisionKind.ESCALATE: (
        "Needs your attention: {task_id} {task_title}",
        (
            "Hello {recipient_name}, {task_id} '{task_title}' ({project}) "
            "needs your attention. {reason_text} Owner: {owner_name}. "
            "Due: {due_date}. Could you help unblock it or agree on a plan?"
        ),
    ),
}


def agent_phrasings(kind: DecisionKind) -> tuple[MessageTemplate, ...]:
    """Return exact approved choices for one permitted action kind."""
    wording = _WORDING.get(kind)
    if wording is None:
        return ()
    subject, body = wording
    return (
        MessageTemplate("agent.v2", subject, body),
        MessageTemplate("agent.v2", subject, body + " Many thanks."),
    )
