"""Deterministic review of an LLM action plan before it can be used.

The guard is the only path from a model's proposal to a decision. It
checks the proposal against the action envelope and the message rules,
and returns either closed violation codes (fed back to the model so it
can repair its plan) or a code-built decision and rendered message.
Facts in the message are rendered by code from placeholders; the
model only chooses words, the action and the recipient reference.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from techcom_followup.domain.agent_messages import agent_phrasings
from techcom_followup.domain.envelope import ActionEnvelope, priority_rank
from techcom_followup.domain.injection import screen
from techcom_followup.domain.messages import (
    LLM_PLACEHOLDERS,
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

AGENT_TEMPLATE_ID = "agent.v2"
AGENT_PLACEHOLDERS = LLM_PLACEHOLDERS | {"owner_name", "reason_text"}
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
    subject: str
    body: str
    rationale: str
    evidence: tuple[str, ...] = ()


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
                *_message_violations(proposal, task),
                *_rationale_violations(
                    proposal, task, note_visible=note_visible
                ),
            ]
        )
    )
    if violations:
        return Review(violations)
    decision = _decision(proposal, envelope)
    facts = facts_for(decision, task, as_of)
    try:
        message = render_draft(
            MessageTemplate(
                AGENT_TEMPLATE_ID, proposal.subject, proposal.body
            ),
            facts,
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


def _message_violations(proposal: PlanProposal, task: Task) -> Iterator[str]:
    if not any(
        proposal.subject == choice.subject and proposal.body == choice.body
        for choice in agent_phrasings(proposal.kind)
    ):
        yield "unapproved_phrasing"
    validator = MessageValidator([task.owner] if task.owner else [])
    yield from validator.violations(
        proposal.subject, proposal.body, allowed=AGENT_PLACEHOLDERS
    )
    if "{task_id}" not in f"{proposal.subject}{proposal.body}":
        yield "missing_task_reference"
    text = UntrustedText.of(f"{proposal.subject}\n{proposal.body}")
    if screen(text).suspicious:
        yield "instruction_like_message"
    if _echoes(task.notes.value, proposal.body):
        yield "note_echo"


def _rationale_violations(
    proposal: PlanProposal, task: Task, *, note_visible: bool
) -> Iterator[str]:
    rationale = proposal.rationale
    if (
        not rationale.strip()
        or len(rationale) > MAX_RATIONALE_CHARS
        or has_contact_or_link(rationale)
        or screen(UntrustedText.of(rationale)).suspicious
    ):
        yield "unsafe_rationale"
    note = task.notes.value
    if any(
        not note_visible
        or len(quote.strip()) < MIN_EVIDENCE_CHARS
        or quote not in note
        or quote not in redact_contacts(note)
        for quote in proposal.evidence
    ):
        yield "ungrounded_evidence"


def _echoes(note: str, body: str) -> bool:
    """Return whether the body repeats a run of words from the note."""
    note_words = _WORD.findall(fold(note))
    runs = {
        tuple(note_words[start : start + NOTE_ECHO_WORDS])
        for start in range(len(note_words) - NOTE_ECHO_WORDS + 1)
    }
    body_words = _WORD.findall(fold(body))
    return any(
        tuple(body_words[start : start + NOTE_ECHO_WORDS]) in runs
        for start in range(len(body_words) - NOTE_ECHO_WORDS + 1)
    )


def _decision(proposal: PlanProposal, envelope: ActionEnvelope) -> Decision:
    baseline = envelope.baseline
    changed = proposal.kind is not baseline.kind
    priority = max(proposal.priority, envelope.min_priority, key=priority_rank)
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
        flags=baseline.flags | {Flag.AGENT_PLANNED},
        interpretation=baseline.interpretation,
        rationale=clean_line(
            f"Selected {proposal.kind.value} for {proposal.recipient} "
            "within the policy envelope.",
            MAX_RATIONALE_CHARS,
        ),
    )
