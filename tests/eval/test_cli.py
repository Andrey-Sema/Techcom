import json
import runpy
from pathlib import Path

import pytest

from eval.cli import main
from eval.contracts import digest_text
from eval.test_harness import bundle
from techcom_followup.domain.models import BlockerCategory


def test_cli_default_replay_and_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    assert main(["--report", str(report)]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["provider_quality"] == "UNVERIFIED"
    assert parsed["gate_failures"] == []
    assert "No provider API was called" in report.read_text()
    assert main(["--report", str(tmp_path / "absent" / "report.md")]) == 2
    assert "invalid_bundle" in capsys.readouterr().err
    assert main(["--require-provider"]) == 2
    assert "invalid_bundle_or_unverified_provider" in capsys.readouterr().err


def test_cli_rejects_missing_bundle_and_module_entrypoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--dataset", "/missing/evaluation.jsonl"]) == 2
    assert "invalid_bundle" in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", ["eval", "--require-provider"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("eval", run_name="__main__")
    assert caught.value.code == 2


def test_cli_returns_failed_gate_without_relabelling_the_pipeline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases, rows, manifest = bundle()
    data = "".join(
        case.model_copy(
            update={"label": BlockerCategory.MATERIALS}
        ).model_dump_json()
        + "\n"
        for case in cases
    )
    recordings = "".join(row.model_dump_json() + "\n" for row in rows)
    metadata = manifest.model_copy(
        update={
            "dataset_sha256": digest_text(data),
            "recordings_sha256": digest_text(recordings),
        }
    )
    paths = [
        tmp_path / name
        for name in ("notes.jsonl", "responses.jsonl", "manifest.json")
    ]
    for path, text in zip(
        paths, (data, recordings, metadata.model_dump_json()), strict=True
    ):
        path.write_text(text)
    args = [
        "--dataset",
        str(paths[0]),
        "--recordings",
        str(paths[1]),
        "--manifest",
        str(paths[2]),
    ]
    assert main(args) == 1
    assert "macro_f1" in json.loads(capsys.readouterr().out)["gate_failures"]
