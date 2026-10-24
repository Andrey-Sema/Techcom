"""Deterministic note interpretation and the "do we need an LLM" gate.

The keyword prefilter resolves the obvious notes without a model call
(the cost lever for 10k tasks a day). Its evidence is the matched
text, copied verbatim, so it passes the same grounding rule as LLM
output.
"""

import re
from datetime import date

from techcom_followup.domain.models import (
    BlockerCategory,
    InterpretationSource,
    NoteInterpretation,
    ScreenResult,
    Task,
    TaskStatus,
    UntrustedText,
)

KEYWORD_CONFIDENCE = 0.95
LLM_SKIP_CONFIDENCE = 0.9

# Order matters: "waiting for materials" is a materials blocker, not
# an upstream dependency, so materials is checked first.
_KEYWORDS: tuple[tuple[BlockerCategory, re.Pattern[str]], ...] = tuple(
    (blocker, re.compile(pattern, re.IGNORECASE))
    for blocker, pattern in (
        (
            BlockerCategory.MATERIALS,
            (
                r"\bback[- ]?order(?:ed)?\b"
                r"|\b(?:awaiting|waiting\s+(?:on|for)|missing|short\s+of)"
                r"\b.{0,40}?\b(?:materials?|parts|shipments?)\b"
                r"|\b(?:materials?|parts|shipments?)\b.{0,40}?"
                r"\b(?:missing|delayed|unavailable)\b"
            ),
        ),
        (
            BlockerCategory.PERMIT,
            (
                r"\bpermits?\b.{0,40}?\b(?:pending|awaiting|waiting|denied"
                r"|delayed|not\s+(?:yet\s+)?(?:issued|approved|granted))\b"
                r"|\b(?:awaiting|waiting\s+(?:on|for)|pending)\b.{0,40}?"
                r"\bpermits?\b"
            ),
        ),
        (
            BlockerCategory.SITE_ACCESS,
            (
                r"\b(?:no\s+site\s+access|site\s+access\s+(?:denied|blocked)"
                r"|locked\s+(?:gate|site)|access\s+(?:denied|blocked))\b"
            ),
        ),
        (
            BlockerCategory.UPSTREAM_DEPENDENCY,
            (
                r"\bupstream\b|\buntil\b.{0,60}?\bconfirmed\b"
                r"|\bwaiting\s+(?:on|for)\b|\bdepends\s+on\b"
            ),
        ),
    )
)

# A word match cannot establish whether a blocker is current. Resolution
# or historical context needs interpretation before confident routing.
# These cues are conservative, not a complete negation detector.
_AMBIGUOUS_CONTEXT = re.compile(
    r"\b(?:historical|formerly|previously|quotes?|quoted|resolved"
    r"|if|unless|hypothetical|potential|example|training|might|could)\b"
    r"|\bold\s+issue\b"
    r"|\bno\s+(?:remaining\s+)?(?:(?:materials?|permit|access|upstream)\s+)?"
    r"(?:blockers?|obstacles?|delays?)\b"
    r"|\b(?:materials?|parts|shipments?)\b.{0,60}"
    r"\b(?:arrived|delivered|received|available)\b"
    r"|\b(?:arrived|delivered|received|available)\b.{0,60}"
    r"\b(?:materials?|parts|shipments?)\b"
    r"|\b(?:materials?|parts|shipments?)\b.{0,60}"
    r"\b(?:on\s+site|in\s+stock|inspection\s+passed)\b"
    r"|\bupstream\b.{0,60}\b(?:complete(?:d)?|finished|cleared)\b"
    r"|\b(?:complete(?:d)?|finished|cleared)\b.{0,60}\bupstream\b",
    re.IGNORECASE | re.DOTALL,
)

# Keywords cannot decide whether a dependency is denied or ETA known.
# Defer negative blockers to a model or the safe UNCLEAR fallback.
_SEMANTIC_CONTEXT = re.compile(
    r"\b(?:not|never|neither|without|isn['\u2019]t|aren['\u2019]t|don['\u2019]t"
    r"|doesn['\u2019]t|cannot|can['\u2019]t)\b"
    r"|\bno\b(?!\s+(?:eta|site\s+access)\b)"
    r"|\b(?:eta|delivery\s+date)\b.{0,30}\b(?:known|confirmed|set)\b"
    r"|\b(?:eta|delivery)\s+(?:is|on|for|by|expected)\s+"
    r"(?:\d|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|\b(?:confirmed|scheduled)\s+delivery\b",
    re.IGNORECASE,
)
_ETA_REFERENCE = re.compile(r"\beta\b|\bdelivery\s+date\b", re.IGNORECASE)
_UNKNOWN_ETA = re.compile(
    r"\bno\s+eta\b|\beta(?:\s+is)?\s+unknown\b"
    r"|\bdelivery\s+date(?:\s+is)?\s+unknown\b",
    re.IGNORECASE,
)


def prefilter(note: UntrustedText) -> NoteInterpretation | None:
    """Classify obvious current blockers; defer ambiguous context."""
    if (
        _AMBIGUOUS_CONTEXT.search(note.value) is not None
        or _SEMANTIC_CONTEXT.search(note.value) is not None
        or _ETA_REFERENCE.search(_UNKNOWN_ETA.sub("", note.value)) is not None
    ):
        return None
    for blocker, pattern in _KEYWORDS:
        match = pattern.search(note.value)
        if match is not None:
            return NoteInterpretation(
                blocker=blocker,
                needs_eta=blocker is BlockerCategory.MATERIALS,
                confidence=KEYWORD_CONFIDENCE,
                evidence=match.group(0),
                source=InterpretationSource.KEYWORD,
            )
    return None


def fallback() -> NoteInterpretation:
    """Return the safe interpretation used when the LLM cannot help.

    ``UNCLEAR`` can only lead to a human being asked; it never turns
    an overdue task into ``no_action``.
    """
    return NoteInterpretation(
        blocker=BlockerCategory.UNCLEAR,
        needs_eta=False,
        confidence=0.0,
        evidence="",
        source=InterpretationSource.FALLBACK,
    )


def needs_llm(
    task: Task,
    screen: ScreenResult,
    keyword_hit: NoteInterpretation | None,
    *,
    as_of: date,
    due_soon_days: int,
) -> bool:
    """Return whether the note is worth a model call.

    The LLM is called only when its answer could change the decision:
    the task is active, has notes, is not flagged, is not already
    decided by an earlier rule (missing data, unknown status), the
    keyword prefilter was not confident, and the task is blocked or
    due soon or overdue.
    """
    if task.status in {TaskStatus.DONE, TaskStatus.UNKNOWN}:
        return False
    if not task.notes or screen.suspicious or task.missing_fields():
        return False
    if keyword_hit is not None:
        return keyword_hit.confidence < LLM_SKIP_CONFIDENCE
    delta = task.days_until_due(as_of)
    return task.status is TaskStatus.BLOCKED or (
        delta is not None and delta <= due_soon_days
    )
