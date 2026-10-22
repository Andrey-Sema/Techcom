"""Offline entry point without network clients or credentials."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eval.contracts import load_bundle
from eval.harness import evaluate
from eval.report import markdown

_DATA = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    """Return success, gate failure or invalid input exits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=_DATA / "notes.jsonl")
    parser.add_argument(
        "--recordings",
        type=Path,
        default=_DATA / "recordings" / "responses.jsonl",
    )
    parser.add_argument(
        "--manifest", type=Path, default=_DATA / "manifest.json"
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require-provider", action="store_true")
    args = parser.parse_args(argv)
    try:
        cases, recordings, manifest = load_bundle(
            args.dataset, args.recordings, args.manifest
        )
        result = asyncio.run(
            evaluate(
                cases,
                recordings,
                manifest,
                require_provider=args.require_provider,
            )
        )
        if args.report is not None:
            args.report.write_text(markdown(result))
    except (OSError, ValueError):
        sys.stderr.write('{"error":"invalid_bundle_or_unverified_provider"}\n')
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "provider_quality": result.provider_quality,
                "scores": result.scores.model_dump(mode="json"),
                "gate_failures": result.gate_failures,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 1 if result.gate_failures else 0
