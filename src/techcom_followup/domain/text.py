"""Text hygiene for values that come from the task source.

Source data is untrusted. Before a value reaches a decision, a log
line or a message it is normalised (NFKC), stripped of control and
format characters (CR/LF injection, bidi overrides, zero-width
characters) and capped in length.
"""

import re
import unicodedata

_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})
_WHITESPACE = re.compile(r"\s+")


def _visible(text: str, *, keep_newlines: bool) -> str:
    return "".join(
        char
        for char in text
        if (keep_newlines and char == "\n")
        or unicodedata.category(char) not in _INVISIBLE_CATEGORIES
        or char in " \t"
    )


def clean_line(value: str, max_chars: int) -> str:
    """Return ``value`` as one tidy line of at most ``max_chars``.

    Args:
        value: Raw text from the task source.
        max_chars: Hard length cap applied after cleaning.

    Returns:
        NFKC-normalised text without control or format characters and
        with every whitespace run collapsed to one space.
    """
    # Line breaks become spaces first, so "a\r\nb" does not glue words.
    text = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value))
    text = _WHITESPACE.sub(" ", _visible(text, keep_newlines=False))
    return text.strip()[:max_chars].rstrip()


def clean_block(value: str, max_chars: int) -> tuple[str, bool]:
    """Clean multi-line text and report whether it was truncated.

    Args:
        value: Raw text from the task source.
        max_chars: Hard length cap applied after cleaning.

    Returns:
        The cleaned text (newlines kept) and a truncation flag.
    """
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _visible(text, keep_newlines=True).strip()
    return text[:max_chars], len(text) > max_chars


def fold(value: str) -> str:
    """Return a comparison key that ignores case and spacing noise."""
    return _WHITESPACE.sub(" ", value).strip().casefold()
