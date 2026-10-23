"""Extra rules for free text written by the orchestrator model.

``MessageValidator`` keeps facts out of drafts by shape: dates, links,
addresses, phone numbers, known names. Free prose needs more, because a
model can state facts without those shapes: "within 2 days", "Bob
Johnson", "the warehouse already shipped it", "boss [at] evil [dot]
com". These rules are deliberately conservative; a rejected plan falls
back to the policy's template, so a false positive costs only wording.

The text is checked after NFKC normalisation, so full-width and other
compatibility forms (a full-width at sign, a one-dot leader) are judged
by what they look like.
"""

import re
import unicodedata
from collections.abc import Iterable, Iterator

from techcom_followup.domain.messages import has_contact_or_link

_TYPOGRAPHY = frozenset("\u2018\u2019\u201c\u201d\u2013\u2014\u2026")
_NUMBER_WORDS = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven"
    r"|twelve|fifteen|twenty|thirty|several|a\s+few|few|couple\s+of"
    r"|a|an)"
)
_UNITS = r"(?:hours?|days?|weeks?|months?|years?)"
_RELATIVE_TIME = re.compile(
    rf"\b(?:within|in|after|for|over)\s+{_NUMBER_WORDS}\s+{_UNITS}\b"
    rf"|\b{_NUMBER_WORDS}\s+{_UNITS}\s+(?:from|ago|late|early|left)\b"
    r"|\b(?:next|this|last|coming)\s+(?:week|month|year|quarter"
    r"|weekend)\b"
    r"|\bend\s+of\s+(?:the\s+)?(?:day|week|month|quarter|year)\b"
    r"|\b(?:EOD|EOW|COB)\b",
    re.IGNORECASE,
)
_UNVERIFIED_CLAIM = re.compile(
    r"\b(?:already|has|have|had|was|were|been|is|are)\s+(?:\w+\s+)?"
    r"(?:shipped|delivered|dispatched|sent|approved|arrived|received"
    r"|completed|finished|cancell?ed|paid|resolved|fixed|installed"
    r"|signed|confirmed|scheduled|booked)\b",
    re.IGNORECASE,
)
_STATE_CLAIM = re.compile(
    r"\bmarked\b.{0,40}?\b(?:as\s+)?(?:complete[d]?|done|closed|resolved)\b",
    re.IGNORECASE,
)
_OBFUSCATED_CONTACT = re.compile(
    r"[\[(<]\s*(?:at|dot)\s*[\])>]"
    r"|\w\s+(?:dot)\s+(?:com|net|org|io|ru|info|biz|app|dev|co|us)\b"
    r"|\b(?:at)\s+\w+\s+dot\s+\w+",
    re.IGNORECASE,
)
_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>|\[[^\]]*\]\([^)]*\)")
# Capitalised words a business message legitimately uses mid-sentence.
_ACRONYMS = frozenset({"I", "ETA", "OK", "PM", "ASAP", "FYI"})
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_SENTENCE = re.compile(r"[.!?:;]+\s*|\n+")
_FULL_NAME_PARTS = 2


def free_text_violations(
    subject: str, body: str, *, known_names: Iterable[str] = ()
) -> tuple[str, ...]:
    """Return closed violation codes for model-written text.

    Placeholders (``{name}``) are facts rendered by code and stand for
    a sentence boundary-neutral word here.
    """
    found: list[str] = []
    for text in (subject, body):
        found.extend(_violations(text, known_names))
    return tuple(dict.fromkeys(found))


def _violations(text: str, known_names: Iterable[str]) -> Iterator[str]:
    normalized = unicodedata.normalize("NFKC", text)
    if any(
        not (char.isascii() and (char.isprintable() or char == "\n"))
        and char not in _TYPOGRAPHY
        for char in normalized
    ):
        yield "non_ascii"
    bare = re.sub(r"\{reason_text\}", ".", normalized)
    bare = re.sub(r"\{[a-z_]+\}", "x", bare)
    if any(char.isdigit() for char in bare):
        yield "digits"
    checks = (
        ("relative_date", _RELATIVE_TIME),
        ("unverified_claim", _UNVERIFIED_CLAIM),
        ("state_change_claim", _STATE_CLAIM),
        ("contact_obfuscation", _OBFUSCATED_CONTACT),
        ("markup", _MARKUP),
    )
    yield from (code for code, pattern in checks if pattern.search(bare))
    if has_contact_or_link(bare):
        yield "contact_obfuscation"
    if _has_proper_noun(bare):
        yield "proper_noun"
    if any(_mentions(bare, name) for name in known_names):
        yield "person_name"


def _has_proper_noun(text: str) -> bool:
    """Flag a capitalised word that does not start a sentence."""
    for sentence in _SENTENCE.split(text):
        words = _WORD.findall(sentence)
        if any(
            word[0].isupper() and word not in _ACRONYMS for word in words[1:]
        ):
            return True
    return False


def _mentions(text: str, name: str) -> bool:
    tokens = name.split()
    if len(tokens) < _FULL_NAME_PARTS:
        return False
    pattern = r"\b" + r"\s+".join(re.escape(token) for token in tokens) + r"\b"
    return re.search(pattern, text, re.IGNORECASE) is not None
