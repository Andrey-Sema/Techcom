"""What the orchestrator model may decide about a message, and no more.

The model chooses message *intents* from closed sets: what to ask for,
the tone, whether to mention an earlier message, and whether a person
should review the action before it is sent. Code turns those choices
into text from reviewed fragments and renders every fact itself, so no
model-written prose ever reaches a recipient (OWASP LLM05, LLM09).
Which intents are allowed depends on facts code verified: a firm tone
needs an unanswered earlier message or an escalation, and a mention of
an earlier message needs one that was actually sent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from techcom_followup.domain.messages import MessageTemplate
from techcom_followup.domain.models import DecisionKind, Task

AGENT_TEMPLATE_ID = "agent.v3"
MAX_ASKS = 2


class Ask(StrEnum):
    """One question the message asks; definition order is text order."""

    STATUS = "status"
    BLOCKER = "blocker"
    UNBLOCK_PLAN = "unblock_plan"
    DELIVERY_ETA = "delivery_eta"
    MISSING_DETAILS = "missing_details"
    OWNER_ASSIGNMENT = "owner_assignment"
    COMPLETION_DATE = "completion_date"
    CONFIRM_ON_TRACK = "confirm_on_track"


class Tone(StrEnum):
    """How insistent the message is."""

    NEUTRAL = "neutral"
    FRIENDLY = "friendly"
    FIRM = "firm"


class Delivery(StrEnum):
    """Send automatically, or keep the action for a person to review."""

    SEND = "send"
    HOLD_FOR_REVIEW = "hold_for_review"


class HoldReason(StrEnum):
    """Why the model asked for a human review; closed for audit."""

    CONFLICTING_SIGNALS = "conflicting_signals"
    UNCLEAR_NOTE = "unclear_note"
    SENSITIVE_ESCALATION = "sensitive_escalation"
    RECIPIENT_UNCERTAIN = "recipient_uncertain"


@dataclass(frozen=True, slots=True)
class MessageOptions:
    """Intents code accepts for one decision kind and task."""

    asks: frozenset[Ask]
    required_any: frozenset[Ask]


@dataclass(frozen=True, slots=True)
class MessagePlan:
    """The model's validated message intents."""

    kind: DecisionKind
    asks: tuple[Ask, ...]
    tone: Tone
    earlier_contact: bool


_ASKS: Mapping[DecisionKind, tuple[frozenset[Ask], frozenset[Ask]]] = {
    DecisionKind.FOLLOW_UP_OWNER: (
        frozenset(
            {
                Ask.STATUS,
                Ask.BLOCKER,
                Ask.COMPLETION_DATE,
                Ask.CONFIRM_ON_TRACK,
            }
        ),
        frozenset({Ask.STATUS, Ask.COMPLETION_DATE, Ask.CONFIRM_ON_TRACK}),
    ),
    DecisionKind.REQUEST_ETA: (
        frozenset({Ask.DELIVERY_ETA, Ask.BLOCKER, Ask.CONFIRM_ON_TRACK}),
        frozenset({Ask.DELIVERY_ETA}),
    ),
    DecisionKind.CLARIFY_MISSING_INFO: (
        frozenset({Ask.MISSING_DETAILS, Ask.OWNER_ASSIGNMENT}),
        frozenset({Ask.MISSING_DETAILS, Ask.OWNER_ASSIGNMENT}),
    ),
    DecisionKind.ESCALATE: (
        frozenset(
            {
                Ask.UNBLOCK_PLAN,
                Ask.STATUS,
                Ask.COMPLETION_DATE,
                Ask.OWNER_ASSIGNMENT,
            }
        ),
        frozenset({Ask.UNBLOCK_PLAN}),
    ),
}

_SUBJECTS: Mapping[DecisionKind, tuple[str, str]] = {
    DecisionKind.FOLLOW_UP_OWNER: (
        "Status check: {task_id} {task_title}",
        "Action needed: {task_id} {task_title}",
    ),
    DecisionKind.REQUEST_ETA: (
        "Delivery ETA needed: {task_id} {task_title}",
        "Delivery ETA still needed: {task_id} {task_title}",
    ),
    DecisionKind.CLARIFY_MISSING_INFO: (
        "Details needed: {task_id} {task_title}",
        "Details still needed: {task_id} {task_title}",
    ),
    DecisionKind.ESCALATE: (
        "Needs your attention: {task_id} {task_title}",
        "Escalation: {task_id} {task_title}",
    ),
}
_CONTEXT: Mapping[DecisionKind, str] = {
    DecisionKind.FOLLOW_UP_OWNER: (
        "I am checking in on {task_id} '{task_title}' ({project}), due "
        "{due_date}. Our tracker shows: {reason_text}"
    ),
    DecisionKind.REQUEST_ETA: (
        "{task_id} '{task_title}' ({project}) is waiting on materials and "
        "is due {due_date}. Our tracker shows: {reason_text}"
    ),
    DecisionKind.CLARIFY_MISSING_INFO: (
        "{task_id} '{task_title}' ({project}) needs clarification. "
        "Our tracker shows: {reason_text}"
    ),
    DecisionKind.ESCALATE: (
        "{task_id} '{task_title}' ({project}) needs your attention. "
        "Our tracker shows: {reason_text} Owner: {owner_name}. "
        "Due: {due_date}."
    ),
}
_GREETING: Mapping[Tone, str] = {
    Tone.NEUTRAL: "Hello {recipient_name},",
    Tone.FRIENDLY: "Hi {recipient_name},",
    Tone.FIRM: "Hello {recipient_name},",
}
_CLOSING: Mapping[Tone, str] = {
    Tone.NEUTRAL: "Thank you.",
    Tone.FRIENDLY: "Thanks a lot for your help!",
    Tone.FIRM: "A reply soon would help us keep the schedule on track.",
}
_EARLIER = "This follows an earlier message we sent about this task."
_QUESTIONS: Mapping[Ask, str] = {
    Ask.STATUS: "Could you share where it stands?",
    Ask.BLOCKER: "Is anything blocking it, and who could unblock it?",
    Ask.COMPLETION_DATE: "What is a realistic completion date?",
    Ask.DELIVERY_ETA: (
        "When will the materials arrive, or who can confirm the delivery date?"
    ),
    Ask.MISSING_DETAILS: "Could you fill in the missing details: "
    "{missing_fields}?",
    Ask.OWNER_ASSIGNMENT: "Who should own this task?",
    Ask.UNBLOCK_PLAN: "Could you help unblock it or agree on a new plan?",
    Ask.CONFIRM_ON_TRACK: "Could you confirm it is still on track?",
}


def message_options(kind: DecisionKind, task: Task) -> MessageOptions:
    """Return the asks code accepts for ``kind`` on this task.

    Asking who should own a task only makes sense when it has no owner.
    """
    allowed, required = _ASKS.get(kind, (frozenset(), frozenset()))
    if task.owner is not None:
        allowed -= {Ask.OWNER_ASSIGNMENT}
        required -= {Ask.OWNER_ASSIGNMENT}
    return MessageOptions(allowed, required)


def firm_tone_allowed(kind: DecisionKind, *, chased: bool) -> bool:
    """Allow insistence after an unanswered message or to escalate."""
    return chased or kind is DecisionKind.ESCALATE


def ordered_asks(asks: tuple[Ask, ...]) -> tuple[Ask, ...]:
    """Deduplicate asks into their fixed reading order."""
    return tuple(ask for ask in Ask if ask in asks)


def compose_agent_message(plan: MessagePlan) -> MessageTemplate:
    """Build a template from reviewed fragments in a fixed order.

    The result still contains placeholders; code renders the facts.
    """
    firm = plan.tone is Tone.FIRM
    sentences = [
        _GREETING[plan.tone],
        _CONTEXT[plan.kind],
        *([_EARLIER] if plan.earlier_contact else []),
        *(_QUESTIONS[ask] for ask in ordered_asks(plan.asks)),
        _CLOSING[plan.tone],
    ]
    return MessageTemplate(
        AGENT_TEMPLATE_ID,
        _SUBJECTS[plan.kind][1 if firm else 0],
        " ".join(sentences),
    )
