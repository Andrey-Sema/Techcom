"""Guard for AC-Q1: comments and docstrings are written in English.

Two rules, checked on every file tracked by git:

* In Python, comments and docstrings may contain ASCII letters and
  Greek math letters (such as "Δ") only. Symbols such as "≤" or "—"
  are fine; words in another language, even in Latin script, are
  not.
* In every text file (code, config, docs), letters must be Latin or
  Greek (Greek covers math notation such as "Δ"). Test data in other
  scripts is written with ``\\u`` escapes, so the source stays clean.
"""

import ast
import io
import subprocess
import tokenize
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

from support import ROOT

TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".txt"}
)
TEXT_NAMES = frozenset({"Dockerfile", "Makefile", ".gitignore"})


def tracked_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 - git from PATH is fine
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [ROOT / name for name in listed.split("\0") if name]


def is_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES


def foreign_letters(text: str, *, ascii_only: bool) -> set[str]:
    """Return letters that break the rule for ``text``."""
    found = set()
    for char in text:
        if not char.isalpha() or char.isascii():
            continue
        allowed = ("GREEK",) if ascii_only else ("LATIN", "GREEK")
        if not unicodedata.name(char, "").startswith(allowed):
            found.add(char)
    return found


def python_prose(source: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line, text)`` for every comment and docstring."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    for node in ast.walk(ast.parse(source)):
        if isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            docstring = ast.get_docstring(node, clean=False)
            if docstring is not None:
                yield node.body[0].lineno, docstring


def python_violations(source: str) -> list[tuple[int, str]]:
    return [
        (line, "".join(sorted(letters)))
        for line, text in python_prose(source)
        if (letters := foreign_letters(text, ascii_only=True))
    ]


def text_violations(source: str) -> list[tuple[int, str]]:
    return [
        (number, "".join(sorted(letters)))
        for number, line in enumerate(source.splitlines(), start=1)
        if (letters := foreign_letters(line, ascii_only=False))
    ]


def test_comments_and_docstrings_are_english() -> None:
    problems = [
        f"{path.relative_to(ROOT)}:{line}: {letters}"
        for path in tracked_files()
        if path.suffix == ".py"
        for line, letters in python_violations(path.read_text("utf-8"))
    ]
    assert problems == []


def test_text_files_use_latin_script_only() -> None:
    problems = [
        f"{path.relative_to(ROOT)}:{line}: {letters}"
        for path in tracked_files()
        if is_text(path)
        for line, letters in text_violations(path.read_text("utf-8"))
    ]
    assert problems == []


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("x = 1  # \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430\n", 1),
        ('def f():\n    """\u0414\u043e\u043a."""\n', 2),
        ('"""Caf\u00e9 notes."""\n', 1),
        ("class A:\n    '''\u6e2c\u8a66'''\n", 2),
    ],
)
def test_guard_catches_non_english_prose(source: str, line: int) -> None:
    assert [found[0] for found in python_violations(source)] == [line]


def test_guard_allows_english_prose_and_foreign_data() -> None:
    source = (
        '"""Due ≤ 1 day — see docs."""\n'
        "# Thresholds: Δ = due - as_of\n"
        'NAME = "J\u00f6hn \u0418\u0432\u0430\u043d"\n'
    )
    assert python_violations(source) == []


def test_text_guard_flags_other_scripts_only() -> None:
    assert text_violations("Δ < 0 → overdue; José\n") == []
    assert text_violations(
        "ok\n# \u043a\u043e\u043c\u043c\u0435\u043d\u0442\n"
    ) == [(2, "\u0435\u043a\u043c\u043d\u043e\u0442")]
