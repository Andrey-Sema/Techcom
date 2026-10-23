"""Deterministic review of an LLM action plan before it can be used.

The guard is the only path from a model's proposal to a decision. It
checks the proposal against the action envelope and returns either
closed violation codes (fed back to the model so it can repair its
plan) or a code-built decision and message. The model chooses the
action, recipient, priority and message intents; code writes every
word from reviewed fragments and renders every fact.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from techcom_followup.domain.agent_messages import (
    MAX_ASKS,
    Ask,
    Delivery,
    HoldReason,
    MessagePlan,
    Tone,
    compose_agent_message,
    ordered_asks,
)
from techcom_followup.domain.envelope import ActionEnvelope, priority_rank
from techcom_followup.domain.free_text import free_text_violations
from techcom_followup.domain.injection import screen
from techcom_followup.domain.messages import (
    TEMPLATE_PLACEHOLDERS,
    DraftSource,
    MessageDraft,
    MessageTemplate,
    MessageValidator,
    facts_for,
    has_contact_or_link,
    redact_contacts,
    render_draft,
)
from techcom_followup.domain.models import (
    Decision,
    DecisionKind,
    Flag,
    Priority,
    ReasonCode,
    Task,
    UntrustedText,
)
from techcom_followup.domain.text import clean_line, fold

MAX_RATIONALE_CHARS = 300
MIN_EVIDENCE_CHARS = 8
NOTE_ECHO_WORDS = 6
_WORD = re.compile(r"\w+")


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A model's proposal; every field is untrusted until reviewed."""

    kind: DecisionKind
    recipient: str
    priority: Priority
    asks: tuple[Ask, ...]
    tone: Tone
    earlier_contact: bool
    rationale: str
    evidence: tuple[str, ...] = ()
    delivery: Delivery = Delivery.SEND
    hold_reason: HoldReason | None = None

    @property
    def message_plan(self) -> MessagePlan:
        """The message intents of this proposal."""
        return MessagePlan(
            self.kind, self.asks, self.tone, self.earlier_contact
        )


@dataclass(frozen=True, slots=True)
class Review:
    """Violations, or the decision and message built from a proposal."""

    violations: tuple[str, ...]
    decision: Decision | None = None
    message: MessageDraft | None = None

    @property
    def accepted(self) -> bool:
        """Return whether the proposal can become an action."""
        return self.decision is not None and self.message is not None


def review(
    proposal: PlanProposal,
    envelope: ActionEnvelope,
    task: Task,
    *,
    as_of: date,
    note_visible: bool,
) -> Review:
    """Check a proposal; on success build its decision and message.

    Args:
        proposal: The model's plan.
        envelope: Choices the policy allows for this task.
        task: The normalized task the plan is about.
        as_of: Evaluation date for rendered facts.
        note_visible: Whether the model saw the note; evidence must
            then be quoted from it, otherwise evidence must be empty.

    Returns:
        A review whose violations are closed, text-free codes.
    """
    violations = tuple(
        dict.fromkeys(
            [
                *_action_violations(proposal, envelope),
                *_intent_violations(proposal, envelope),
                *_rationale_violations(
                    proposal, task, note_visible=note_visible
                ),
            ]
        )
    )
    if violations:
        return Review(violations)
    template = compose_agent_message(proposal.message_plan)
    unsafe = _fragment_violations(template, task)
    if unsafe:
        return Review(unsafe)
    decision = _decision(proposal, envelope)
    try:
        message = render_draft(
            template,
            facts_for(decision, task, as_of),
            source=DraftSource.LLM,
        )
    except ValueError:
        return Review(("rendered_length",))
    return Review((), decision, message)


def _action_violations(
    proposal: PlanProposal, envelope: ActionEnvelope
) -> Iterator[str]:
    if not envelope.permits_kind(proposal.kind):
        yield "decision_not_permitted"
    elif not envelope.permits(proposal.kind, proposal.recipient):
        yield "recipient_not_permitted"
    if priority_rank(proposal.priority) < priority_rank(envelope.min_priority):
        yield "priority_lowered"


def _ask_violations(
    proposal: PlanProposal, envelope: ActionEnvelope
) -> Iterator[str]:
    asks = proposal.asks
    if not asks:
        yield "missing_ask"
    if len(asks) > MAX_ASKS:
        yield "too_many_asks"
    if len(set(asks)) != len(asks):
        yield "duplicate_ask"
    options = envelope.messages.get(proposal.kind)
    if options is not None:
        if not set(asks) <= options.asks:
            yield "ask_not_permitted"
        if not set(asks) & options.required_any:
            yield "missing_primary_ask"


def _intent_violations(
    proposal: PlanProposal, envelope: ActionEnvelope
) -> Iterator[str]:
    yield from _ask_violations(proposal, envelope)
    if envelope.permits_kind(
        proposal.kind
    ) and proposal.tone not in envelope.tones(proposal.kind):
        yield "tone_not_permitted"
    if proposal.earlier_contact and not envelope.earlier_contact:
        # Saying "we wrote before" must be a verified fact (LLM09).
        yield "unverified_earlier_contact"
    if (proposal.delivery is Delivery.HOLD_FOR_REVIEW) != (
        proposal.hold_reason is not None
    ):
        yield "hold_reason_mismatch"


def _fragment_violations(
    template: MessageTemplate, task: Task
) -> tuple[str, ...]:
    """Hold code-owned fragments to the same fact rules as any text."""
    names = [task.owner] if task.owner else []
    found = [
        *MessageValidator(names).violations(
            template.subject, template.body, allowed=TEMPLATE_PLACEHOLDERS
        ),
        *free_text_violations(
            template.subject, template.body, known_names=names
        ),
    ]
    return tuple(dict.fromkeys(f"fragment_{code}" for code in found))


def _rationale_violations(
    proposal: PlanProposal, task: Task, *, note_visible: bool
) -> Iterator[str]:
    note = task.notes.value
    if _unsafe_rationale(proposal.rationale, note):
        yield "unsafe_rationale"
    if any(
        not note_visible
        or len(quote.strip()) < MIN_EVIDENCE_CHARS
        or quote not in note
        or quote not in redact_contacts(note)
        for quote in proposal.evidence
    ):
        yield "ungrounded_evidence"


def _unsafe_rationale(rationale: str, note: str) -> bool:
    """Reject a rationale that could leak, instruct or store the note.

    The rationale is stored as the model's opinion; raw notes never are.
    """
    return (
        not rationale.strip()
        or len(rationale) > MAX_RATIONALE_CHARS
        or has_contact_or_link(rationale)
        or screen(UntrustedText.of(rationale)).suspicious
        or _echoes(note, rationale)
    )


def _echoes(note: str, text: str) -> bool:
    """Return whether ``text`` repeats a run of words from the note."""
    note_words = _WORD.findall(fold(note))
    runs = {
        tuple(note_words[start : start + NOTE_ECHO_WORDS])
        for start in range(len(note_words) - NOTE_ECHO_WORDS + 1)
    }
    words = _WORD.findall(fold(text))
    return any(
        tuple(words[start : start + NOTE_ECHO_WORDS]) in runs
        for start in range(len(words) - NOTE_ECHO_WORDS + 1)
    )


def _decision(proposal: PlanProposal, envelope: ActionEnvelope) -> Decision:
    baseline = envelope.baseline
    changed = proposal.kind is not baseline.kind
    priority = max(proposal.priority, envelope.min_priority, key=priority_rank)
    held = proposal.delivery is Delivery.HOLD_FOR_REVIEW
    asks = ", ".join(ordered_asks(proposal.asks))
    summary = (
        f"Agent chose {proposal.kind.value} for {proposal.recipient}, "
        f"asking for {asks} in a {proposal.tone.value} tone"
    )
    if held:
        summary += f"; held for review ({proposal.hold_reason})"
    return Decision(
        task_id=baseline.task_id,
        kind=proposal.kind,
        target=envelope.target(proposal.recipient),
        rule_id=f"{baseline.rule_id}+agent",
        reason_code=(
            ReasonCode.AGENT_JUDGEMENT if changed else baseline.reason_code
        ),
        reason=baseline.reason,
        priority=priority,
        flags=baseline.flags
        | {Flag.AGENT_PLANNED}
        | ({Flag.HELD_FOR_REVIEW} if held else set()),
        interpretation=baseline.interpretation,
        rationale=clean_line(f"{summary}.", MAX_RATIONALE_CHARS),
    )
