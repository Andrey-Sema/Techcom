"""The demo smoke check catches drift but ignores per-run ids."""

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

from support import ROOT


def load() -> ModuleType:
    path = ROOT / "scripts" / "demo_check.py"
    spec = importlib.util.spec_from_file_location("demo_check", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load()
SAMPLES = ROOT / "samples"


def copy_samples(tmp_path: Path) -> Path:
    demo = tmp_path / "demo"
    shutil.copytree(SAMPLES, demo)
    return demo


def edit(path: Path, change: object) -> None:
    report = json.loads(path.read_text(encoding="utf-8"))
    report["actions"][0].update(change)
    path.write_text(json.dumps(report), encoding="utf-8")


def test_identifiers_and_receipts_may_change(tmp_path: Path) -> None:
    demo = copy_samples(tmp_path)
    edit(
        demo / "run_report.json",
        {"id": "other", "provider_ref": "mock-99", "decision_id": "x"},
    )
    assert CHECK.main([str(demo), str(SAMPLES)]) == 0


def test_a_changed_status_is_drift(tmp_path: Path) -> None:
    demo = copy_samples(tmp_path)
    edit(demo / "run_report.json", {"status": "dead"})
    assert CHECK.differences(demo, SAMPLES) == ["run_report.json"]
    assert CHECK.main([str(demo), str(SAMPLES)]) == 1
