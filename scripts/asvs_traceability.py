"""Render docs/asvs-traceability.md from docs/15 and pytest markers.

The checklist in ``docs/15`` (section 4) owns the status of every ASVS
requirement; tests claim evidence with ``@pytest.mark.asvs("x.y.z")``.
This script joins the two and reports inconsistencies:

* a marker without an id, or with an id the checklist does not list;
* a requirement marked *Built* without a single test;
* evidence attached to an *N/A* requirement.

Usage: ``python scripts/asvs_traceability.py [--check]``. ``--check``
writes nothing and fails when the report is stale or inconsistent; the
meta test ``tests/meta/test_asvs_traceability.py`` runs the same check.
"""

import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKLIST = ROOT / "docs" / "15-security-asvs-l2.md"
REPORT = ROOT / "docs" / "asvs-traceability.md"
TESTS = ROOT / "tests"
_ID = re.compile(r"\d+\.\d+\.\d+")
_RANGE = re.compile(r"(\d+\.\d+)\.(\d+)\u2013\1\.(\d+)")


@dataclass(frozen=True, slots=True)
class Requirement:
    """One checklist row; ``ids`` expands lists and ranges."""

    label: str
    ids: tuple[str, ...]
    control: str
    status: str

    @property
    def state(self) -> str:
        """The leading status word: Built, Planned, Prod, Doc, N/A..."""
        match = re.match(r"N/A|[A-Za-z]+", self.status)
        return match.group(0) if match else self.status


@dataclass(frozen=True, slots=True)
class Evidence:
    """Tests per requirement id plus markers that name no id."""

    by_id: dict[str, list[str]]
    bare: list[str]


def expand(label: str) -> tuple[str, ...]:
    """Expand id lists and en-dash ranges; ``x.y.x`` rows add none."""
    ids: list[str] = []
    for part in (piece.strip() for piece in label.split(",")):
        span = _RANGE.fullmatch(part)
        if span:
            prefix, first, last = span.groups()
            ids += [f"{prefix}.{n}" for n in range(int(first), int(last) + 1)]
        elif _ID.fullmatch(part):
            ids.append(part)
    return tuple(ids)


def parse_checklist(text: str) -> list[Requirement]:
    """Read the requirement tables of section 4."""
    section = text.split("## 4.", 1)[1].split("\n## 5.", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.match(r"\d", cells[0]):  # noqa: PLR2004
            continue
        rows.append(
            Requirement(cells[0], expand(cells[0]), cells[2], cells[-1])
        )
    return rows


def _asvs_ids(decorator: ast.expr) -> list[str] | None:
    """Ids of an ``asvs`` marker: ``[]`` if bare, ``None`` if other."""
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call else decorator
    if not (isinstance(target, ast.Attribute) and target.attr == "asvs"):
        return None
    args = call.args if call else []
    return [
        arg.value
        for arg in args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _module_marks(tree: ast.Module) -> list[ast.expr]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark"
            for t in node.targets
        ):
            value = node.value
            return list(value.elts) if isinstance(value, ast.List) else [value]
    return []


def collect(tests: Path = TESTS) -> Evidence:
    """Find ``asvs`` markers on test functions, classes and modules."""
    evidence = Evidence(defaultdict(list), [])
    for path in sorted(tests.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.relative_to(tests.parent).as_posix()
        inherited = _module_marks(tree)
        for node in tree.body:
            _visit(node, module, inherited, evidence)
    return evidence


def _visit(
    node: ast.stmt, prefix: str, inherited: list[ast.expr], evidence: Evidence
) -> None:
    if isinstance(node, ast.ClassDef):
        marks = inherited + node.decorator_list
        for child in node.body:
            _visit(child, f"{prefix}::{node.name}", marks, evidence)
        return
    is_test = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    if not is_test or not node.name.startswith("test_"):
        return
    test = f"{prefix}::{node.name}"
    for decorator in inherited + node.decorator_list:
        ids = _asvs_ids(decorator)
        if ids == []:
            evidence.bare.append(test)
        for requirement_id in ids or []:
            evidence.by_id[requirement_id].append(test)


def problems(rows: list[Requirement], evidence: Evidence) -> list[str]:
    """Every inconsistency between the checklist and the markers."""
    known = {rid for row in rows for rid in row.ids}
    found = [f"marker without an ASVS id: {test}" for test in evidence.bare]
    found += [
        f"unknown ASVS id {rid} in {', '.join(tests)}"
        for rid, tests in sorted(evidence.by_id.items())
        if rid not in known
    ]
    for row in rows:
        tests = [t for rid in row.ids for t in evidence.by_id.get(rid, [])]
        if row.state == "Built" and not tests:
            found.append(f"{row.label} is Built but has no test")
        if row.state == "N/A" and tests:
            found.append(f"{row.label} is N/A but has tests")
    return found


def render(rows: list[Requirement], evidence: Evidence) -> str:
    """Markdown report: summary, Built evidence, work in progress."""
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.state] += 1
    lines = [
        "# ASVS 5.0 traceability",
        "",
        "Generated by `scripts/asvs_traceability.py` from the checklist in",
        "[docs/15](15-security-asvs-l2.md) and the `@pytest.mark.asvs`",
        "markers. Do not edit by hand; the meta test fails when it is",
        "stale.",
        "",
        "| Status | Rows |",
        "|--------|------|",
        *(f"| {state} | {counts[state]} |" for state in sorted(counts)),
    ]
    for title, wanted in (
        ("Built: requirement → tests", {"Built"}),
        ("Evidence for requirements still in progress", {"Planned"}),
    ):
        lines += ["", f"## {title}", "", "| ID | Control | Tests |"]
        lines.append("|----|---------|-------|")
        for row in rows:
            tests = sorted(
                {t for rid in row.ids for t in evidence.by_id.get(rid, [])}
            )
            if row.state in wanted and tests:
                cell = "<br>".join(f"`{test}`" for test in tests)
                lines.append(f"| {row.label} | {row.control} | {cell} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    """Write the report, or with ``--check`` verify it."""
    rows = parse_checklist(CHECKLIST.read_text(encoding="utf-8"))
    evidence = collect()
    issues = problems(rows, evidence)
    report = render(rows, evidence)
    if "--check" in argv:
        current = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
        if current != report:
            issues.append(f"{REPORT.name} is stale: run this script")
    else:
        REPORT.write_text(report, encoding="utf-8")
    for issue in issues:
        print(issue)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
