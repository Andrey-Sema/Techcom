"""Screen untrusted notes for instruction-like content (OWASP LLM01).

Detection is a *signal*, not the defence: a flagged note is kept away
from the LLM and messages fall back to templates, but the decision is
still computed from dates and status. The layers that actually hold
(closed output schema, no tools, code-owned facts) do not depend on
this screen catching everything.
"""

import re
import unicodedata
from collections.abc import Mapping

from techcom_followup.domain.models import ScreenResult, Task, UntrustedText

_PATTERNS: Mapping[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for name, pattern in {
        "ignore_instructions": (
            r"\b(?:ignore|forget|override)\s+(?:all\s+|any\s+)?"
            r"(?:(?:the|your)\s+)?(?:(?:previous|prior|above|earlier"
            r"|preceding|system)\s+)?(?:instructions?|prompts?|rules"
            r"|directions)\b"
        ),
        "disregard": (
            r"\bdisregard\s+(?:all|any|the|your|previous|prior|above"
            r"|earlier)\b"
        ),
        "system_prompt": r"\bsystem\s+prompt\b",
        "role_change": r"\byou\s+are\s+now\b|\bact\s+as\s+(?:an?\s+)?ai\b",
        "state_change_command": (
            r"\bmark\b.{0,40}?\b(?:as\s+)?(?:complete[d]?|done|closed"
            r"|resolved)\b"
        ),
        "new_instructions": (r"\b(?:new|updated|real)\s+instructions?\b"),
        "role_tag": (
            r"<\s*/?\s*(?:system|assistant|developer|user|task_note)\b"
            r"|\[/?(?:inst|sys)\]|<\|im_(?:start|end)\|>"
            r"|^\s*(?:system|assistant|developer)\s*:"
        ),
    }.items()
}


def screen(note: UntrustedText) -> ScreenResult:
    """Return which injection patterns, if any, the note matches.

    The note is NFKC-normalised again before matching so that
    full-width and other compatibility look-alikes are caught.
    """
    text = unicodedata.normalize("NFKC", note.value)
    matched = tuple(
        name for name, pattern in _PATTERNS.items() if pattern.search(text)
    )
    return ScreenResult(suspicious=bool(matched), matched_patterns=matched)


def screen_task(task: Task) -> ScreenResult:
    """Screen every untrusted text field that a model could see.

    The title and project name come from the same tracker as the note;
    an instruction hidden in any of them flags the whole task.
    """
    fields = (task.notes.value, task.title, task.project)
    return screen(UntrustedText("\n".join(fields)))
