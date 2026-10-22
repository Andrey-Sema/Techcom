"""The ASVS traceability report is current and consistent (AC-S2).

``scripts/asvs_traceability.py`` joins the checklist in ``docs/15``
with the ``@pytest.mark.asvs`` markers; a *Built* requirement without a
test, an unknown id or a stale report fails the suite.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from support import ROOT


def load() -> ModuleType:
    path = ROOT / "scripts" / "asvs_traceability.py"
    spec = importlib.util.spec_from_file_location("asvs_traceability", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ASVS = load()
ROWS = ASVS.parse_checklist(ASVS.CHECKLIST.read_text(encoding="utf-8"))


def test_checklist_and_markers_agree() -> None:
    assert ASVS.problems(ROWS, ASVS.collect()) == []


def test_report_is_current() -> None:
    expected = ASVS.render(ROWS, ASVS.collect())
    assert ASVS.REPORT.read_text(encoding="utf-8") == expected, (
        "run: uv run python scripts/asvs_traceability.py"
    )


def test_every_built_row_is_counted() -> None:
    built = [row.label for row in ROWS if row.state == "Built"]
    assert built
    assert f"| Built | {len(built)} |" in ASVS.REPORT.read_text(
        encoding="utf-8"
    )


def test_lists_and_ranges_expand() -> None:
    assert ASVS.expand("1.2.6\N{EN DASH}1.2.8, 1.3.4") == (
        "1.2.6",
        "1.2.7",
        "1.2.8",
        "1.3.4",
    )
    assert ASVS.expand("3.3.x, 6.2.x") == ()


def test_inconsistencies_are_reported(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "import pytest\n"
        'pytestmark = pytest.mark.asvs("9.9.9")\n'
        "@pytest.mark.asvs\n"
        "def test_bare(): ...\n"
        "class TestGroup:\n"
        '    @pytest.mark.asvs("1.2.6")\n'
        "    def test_na(self): ...\n"
        "def helper(): ...\n",
        encoding="utf-8",
    )
    rows = [
        ASVS.Requirement("1.1.1", ("1.1.1",), "control", "Built"),
        ASVS.Requirement("1.2.6\N{EN DASH}1.2.8", ("1.2.6",), "none", "N/A"),
    ]
    assert ASVS.problems(rows, ASVS.collect(tests)) == [
        "marker without an ASVS id: tests/test_sample.py::test_bare",
        (
            "unknown ASVS id 9.9.9 in tests/test_sample.py::test_bare, "
            "tests/test_sample.py::TestGroup::test_na"
        ),
        "1.1.1 is Built but has no test",
        "1.2.6\N{EN DASH}1.2.8 is N/A but has tests",
    ]
