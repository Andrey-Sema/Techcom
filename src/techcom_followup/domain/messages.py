"""Message templates, placeholder rendering and the message validator.

Facts (names, dates, IDs, counts) are inserted by code. A language
model may only phrase text around allow-listed placeholders, and its
draft is rejected if it states a fact itself. Rendering is a single
regex pass over an allow-listed mapping, never ``str.format``, so a
value like ``{recipient_name.__class__}`` cannot reach Python
attribute lookup (format-string injection).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from techcom_followup.domain.injection import screen
from techcom_followup.domain.models import (
    Decision,
    ReasonCode,
    Task,
    UntrustedText,
)
from techcom_followup.domain.text import clean_line

SUBJECT_MAX_CHARS = 120
BODY_MIN_CHARS = 20
BODY_MAX_CHARS = 800
MAX_FACT_CHARS = 80  # Per-fact bound; final messages have separate limits.
MIN_NAME_TOKEN_CHARS = 3  # shorter tokens are initials or particles

LLM_PLACEHOLDERS = frozenset(
    {
        "recipient_name",
        "task_id",
        "task_title",
        "project",
        "due_date",
        "days_overdue",
        "missing_fields",
    }
)
# Code-owned facts that templates may use but an LLM is never offered.
TEMPLATE_PLACEHOLDERS = LLM_PLACEHOLDERS | {"owner_name", "reason_text"}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_BRACED = re.compile(r"\{([^{}]*)\}")
_URL = re.compile(
    r"\b[a-z][a-z0-9+.-]*://\S*|\b(?:javascript|data|mailto):\S+"
    r"|\bwww\.\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:[^\W\d_]{2,63}|xn--[a-z0-9-]+)"
    r"\b(?:/\S*)?",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?\b"
    rf"|\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\b"
    r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|today|tomorrow|yesterday|tonight)\b",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"\d{3,}")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_CALENDAR_VALUE_PATTERN = (
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?)?"
)
_CALENDAR_VALUE = re.compile(rf"\b{_CALENDAR_VALUE_PATTERN}(?!\d)")
_CALENDAR_RANGE = re.compile(
    rf"\b{_CALENDAR_VALUE_PATTERN}"
    rf"(?:\s+-\s+{_CALENDAR_VALUE_PATTERN})*(?!\d)"
)
_STATE_CLAIM = re.compile(
    r"\bmarked\s+(?:as\s+)?(?:complete[d]?|done|closed|resolved)\b"
    r"|\bhas\s+been\s+(?:closed|completed|resolved|cancell?ed)\b"
    r"|\bstatus\s+(?:has\s+been\s+|was\s+)?(?:changed|updated|set)\b",
    re.IGNORECASE,
)
_CONTROL = re.compile("[\\x00-\\x1f\\x7f\\u200b-\\u200f\\u202a-\\u202e]")


class DraftSource(StrEnum):
    """Who wrote the wording of a message."""

    TEMPLATE = "template"
    LLM = "llm"


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    """A versioned subject/body pair with placeholders."""

    template_id: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """A rendered message, ready for the outbox."""

    subject: str
    body: str
    template_id: str
    source: DraftSource


_TEMPLATE_LIST = (
    MessageTemplate(
        "follow_up.overdue.v1",
        "Follow-up: {task_id} {task_title}",
        "Hi {recipient_name}, task {task_id} '{task_title}' ({project}) "
        "was due on {due_date} and is {days_overdue} day(s) overdue. "
        "Could you share the current status and a realistic completion "
        "date?",
    ),
    MessageTemplate(
        "follow_up.blocked.v1",
        "Blocker details needed: {task_id} {task_title}",
        "Hi {recipient_name}, {task_id} '{task_title}' ({project}) is "
        "blocked, but the reason is not clear. Could you describe what "
        "is blocking it and who can unblock it?",
    ),
    MessageTemplate(
        "follow_up.due_soon.v1",
        "Reminder: {task_id} {task_title}",
        "Hi {recipient_name}, a reminder that {task_id} '{task_title}' "
        "({project}) is due on {due_date}. Is it on track?",
    ),
    MessageTemplate(
        "request_eta.v1",
        "ETA needed: {task_id} {task_title}",
        "Hello {recipient_name}, {task_id} '{task_title}' ({project}) is "
        "blocked waiting for materials. Please confirm the expected "
        "delivery date.",
    ),
    MessageTemplate(
        "clarify.v1",
        "Missing information: {task_id} {task_title}",
        "Hello {recipient_name}, {task_id} '{task_title}' ({project}) is "
        "missing: {missing_fields}. Please update it so we can track it "
        "correctly.",
    ),
    MessageTemplate(
        "follow_up.materials_eta_known.v1",
        "Materials follow-up: {task_id} {task_title}",
        "Hello {recipient_name}, {task_id} '{task_title}' ({project}) "
        "has a materials dependency with an ETA. Could you confirm "
        "whether the planned delivery and task are still on track?",
    ),
    MessageTemplate(
        "escalate.v1",
        "Escalation: {task_id} {task_title}",
        "Hello {recipient_name}, {task_id} '{task_title}' ({project}) "
        "needs attention: {reason_text} Owner: {owner_name}. "
        "Due: {due_date}.",
    ),
)
TEMPLATES: Mapping[str, MessageTemplate] = {
    template.template_id: template for template in _TEMPLATE_LIST
}

_TEMPLATE_BY_REASON: Mapping[ReasonCode, str] = {
    ReasonCode.MISSING_OWNER: "clarify.v1",
    ReasonCode.MISSING_DUE_DATE: "clarify.v1",
    ReasonCode.MISSING_OWNER_AND_DUE_DATE: "clarify.v1",
    ReasonCode.UNKNOWN_STATUS: "clarify.v1",
    ReasonCode.MATERIALS_ETA_UNKNOWN: "request_eta.v1",
    ReasonCode.MATERIALS_ETA_KNOWN: "follow_up.materials_eta_known.v1",
    ReasonCode.BLOCKED_DEPENDENCY: "escalate.v1",
    ReasonCode.OVERDUE_ESCALATION: "escalate.v1",
    ReasonCode.OVERDUE_NO_EXPLANATION: "follow_up.overdue.v1",
    ReasonCode.BLOCKED_REASON_UNCLEAR: "follow_up.blocked.v1",
    ReasonCode.DUE_SOON_NOT_STARTED: "follow_up.due_soon.v1",
    ReasonCode.DUE_SOON_STALE: "follow_up.due_soon.v1",
}


def template_for(decision: Decision) -> MessageTemplate:
    """Return the template for an actionable decision.

    Raises:
        ValueError: If the decision does not lead to a message.
    """
    template_id = _TEMPLATE_BY_REASON.get(decision.reason_code)
    if template_id is None or decision.target is None:
        msg = f"{decision.reason_code} decisions have no message"
        raise ValueError(msg)
    return TEMPLATES[template_id]


def facts_for(decision: Decision, task: Task, as_of: date) -> dict[str, str]:
    """Return every placeholder value, computed from data, not text.

    Values are scrubbed: control characters, braces, links and e-mail
    addresses from the task source never reach a message.
    """
    target = decision.target
    delta = task.days_until_due(as_of)
    missing = [name.replace("_", " ") for name in task.missing_fields()]
    raw = {
        "recipient_name": "there" if target is None else target.name,
        "task_id": task.task_id,
        "task_title": task.title,
        "project": task.project,
        "due_date": (
            "not set" if task.due_date is None else task.due_date.isoformat()
        ),
        "days_overdue": str(max(0, -delta) if delta is not None else 0),
        "missing_fields": ", ".join(missing) or "a recognised status",
        "owner_name": task.owner or "unassigned",
        "reason_text": decision.reason,
    }
    return {
        name: _scrub(
            value,
            include_phone=name not in {"task_id", "due_date", "days_overdue"},
        )
        for name, value in raw.items()
    }


def render(text: str, facts: Mapping[str, str]) -> str:
    """Replace ``{name}`` placeholders in one pass.

    Raises:
        ValueError: If ``text`` uses a placeholder missing in ``facts``.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in facts:
            msg = f"unknown placeholder: {name}"
            raise ValueError(msg)
        return facts[name]

    return _PLACEHOLDER.sub(substitute, text)


def compose(decision: Decision, task: Task, as_of: date) -> MessageDraft:
    """Render the template message for an actionable decision."""
    template = template_for(decision)
    facts = facts_for(decision, task, as_of)
    return render_draft(template, facts, source=DraftSource.TEMPLATE)


def render_draft(
    template: MessageTemplate,
    facts: Mapping[str, str],
    *,
    source: DraftSource,
) -> MessageDraft:
    """Render every message path within the delivery contract.

    Long subjects use a code-owned compact form; the complete task ID
    remains in the subject and task details remain in the body.

    Raises:
        ValueError: Rendered content cannot meet the delivery bounds.
    """
    subject = render(template.subject, facts)
    body = render(template.body, facts)
    if len(subject) > SUBJECT_MAX_CHARS:
        subject = f"Task {facts['task_id']}: action required"
    if (
        not 1 <= len(subject) <= SUBJECT_MAX_CHARS
        or not BODY_MIN_CHARS <= len(body) <= BODY_MAX_CHARS
    ):
        message = "rendered message exceeds delivery bounds"
        raise ValueError(message)
    return MessageDraft(subject, body, template.template_id, source)


class MessageValidator:
    """Rejects drafts that state facts instead of using placeholders.

    It checks *unrendered* text (an LLM draft or a template). Each
    violation is a short code so that metrics and audit records never
    need to carry the draft text itself.
    """

    def __init__(self, known_names: Iterable[str] = ()) -> None:
        """Remember person names that must only appear via placeholder.

        Args:
            known_names: Owner names from the current task set.
        """
        tokens = {
            token
            for name in known_names
            for token in name.split()
            if len(token) >= MIN_NAME_TOKEN_CHARS
        }
        self._names = tuple(
            re.compile(rf"\b{re.escape(token)}\b") for token in sorted(tokens)
        )

    def violations(
        self,
        subject: str,
        body: str,
        allowed: frozenset[str] = LLM_PLACEHOLDERS,
    ) -> tuple[str, ...]:
        """Return the violation codes of a draft (empty when valid)."""
        found: list[str] = []
        if not 1 <= len(subject) <= SUBJECT_MAX_CHARS:
            found.append("subject_length")
        if not BODY_MIN_CHARS <= len(body) <= BODY_MAX_CHARS:
            found.append("body_length")
        if _CONTROL.search(subject) or _CONTROL.search(body.replace("\n", "")):
            found.append("control_characters")
        text = f"{subject}\n{body}"
        names = {match.group(1) for match in _BRACED.finditer(text)}
        if names - allowed:
            found.append("unknown_placeholder")
        # A word stand-in keeps adjacency visible: "{project}.com" must
        # still read as a host name, as it will once rendered.
        found.extend(self._content_violations(_BRACED.sub("x", text)))
        return tuple(found)

    def _content_violations(self, bare: str) -> list[str]:
        checks = (
            ("stray_brace", re.compile(r"[{}]")),
            ("url", _URL),
            ("email", _EMAIL),
            ("literal_date", _DATE),
            ("phone", _PHONE),
            ("digits", _DIGITS),
            ("state_change_claim", _STATE_CLAIM),
        )
        found = [code for code, pattern in checks if pattern.search(bare)]
        if any(pattern.search(bare) for pattern in self._names):
            found.append("person_name")
        return found


def has_contact_or_link(text: str) -> bool:
    """Return whether ``text`` has a link, address or phone number."""
    return any(pattern.search(text) for pattern in (_URL, _EMAIL, _PHONE))


def redact_contacts(text: str, *, include_phone: bool = True) -> str:
    """Minimise contact data before model exposure or rendering."""
    text = _EMAIL.sub("[address removed]", text)
    text = _URL.sub("[link removed]", text)
    if not include_phone:
        return text
    # Only a complete phone candidate inside a valid temporal span is
    # exempt. A country code or extra phone digits cannot borrow that
    # exemption merely by containing a date-shaped substring.
    spans = _calendar_spans(text)
    return _PHONE.sub(
        lambda match: (
            match.group(0)
            if any(
                start <= match.start() and match.end() <= end
                for start, end in spans
            )
            else "[phone removed]"
        ),
        text,
    )


def _calendar_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans = []
    for match in _CALENDAR_RANGE.finditer(text):
        try:
            for value in _CALENDAR_VALUE.finditer(match.group(0)):
                datetime.fromisoformat(value.group(0))
        except ValueError:
            continue
        spans.append(match.span())
    return tuple(spans)


def _scrub(value: str, *, include_phone: bool = True) -> str:
    text = clean_line(value, MAX_FACT_CHARS * 4)
    text = text.replace("{", "(").replace("}", ")")
    if screen(UntrustedText.of(text)).suspicious:
        return "[untrusted text withheld]"
    text = redact_contacts(text, include_phone=include_phone)
    return clean_line(text, MAX_FACT_CHARS)
