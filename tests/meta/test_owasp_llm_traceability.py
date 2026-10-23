"""The OWASP LLM Top 10 matrix and the test markers stay in step."""

import ast
import re
from pathlib import Path

from support import ROOT

DOC = ROOT / "docs/security/owasp-llm-top10.md"
RISKS = tuple(f"LLM{number:02d}" for number in range(1, 11))
_ROW = re.compile(r"^\| (LLM\d\d) [^|]+\|(?:[^|]*\|){2}([^|]*)\|([^|]*)\|$")
_CITATION = re.compile(r"`(tests/[\w/]+\.py)::(\w+)`")


def rows() -> dict[str, tuple[str, str]]:
    found = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            found[match[1]] = (match[2], match[3].strip())
    return found


def markers(path: Path) -> dict[str, set[str]]:
    """Map each test function in ``path`` to its OWASP LLM risks."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        risks = found.setdefault(node.name, set())
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "owasp_llm"
            ):
                risks.update(
                    arg.value
                    for arg in decorator.args
                    if isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                )
    return found


def test_every_risk_has_exactly_one_row() -> None:
    assert sorted(rows()) == list(RISKS)


def test_every_cited_test_exists_and_carries_its_risk() -> None:
    for risk, (evidence, _status) in rows().items():
        for path, name in _CITATION.findall(evidence):
            tagged = markers(ROOT / path)
            assert name in tagged, f"{risk}: {path}::{name} is missing"
            assert risk in tagged[name], f"{path}::{name} lacks {risk}"


def test_mitigated_risks_have_evidence_and_others_say_why() -> None:
    for risk, (evidence, status) in rows().items():
        if status.startswith("Mitigated"):
            assert _CITATION.search(evidence), risk
        else:
            assert status == "N/A", risk


def test_every_marker_names_a_known_risk() -> None:
    for path in (ROOT / "tests").rglob("test_*.py"):
        for risks in markers(path).values():
            assert risks <= set(RISKS), (path, risks)
