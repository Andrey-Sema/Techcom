"""Compare a fresh ``make demo`` with the committed samples (CI smoke).

Identifiers, receipts and receiver timestamps differ between executions
(``samples/README.md``); everything else must match exactly, so a change
in decisions, messages, statuses or dedupe counters fails the job.

Usage: ``python scripts/demo_check.py [demo_dir] [samples_dir]``.
"""

import json
import sys
from pathlib import Path

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None

VOLATILE = frozenset(
    {
        "id",
        "decision_id",
        "next_attempt_at",
        "provider_ref",
    }
)
FILES = ("run_report.json", "decisions.json")


def stable(value: Json) -> Json:
    """Drop per-execution fields; keep everything that is a decision."""
    if isinstance(value, dict):
        return {
            key: stable(item)
            for key, item in value.items()
            if key not in VOLATILE
        }
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def differences(demo: Path, samples: Path) -> list[str]:
    """Names of the files whose stable projection differs."""
    found = []
    for name in FILES:
        fresh = json.loads((demo / name).read_text(encoding="utf-8"))
        kept = json.loads((samples / name).read_text(encoding="utf-8"))
        if stable(fresh) != stable(kept):
            found.append(name)
    return found


def main(argv: list[str]) -> int:
    """Return non-zero and name the files when the demo drifted."""
    demo = Path(argv[0]) if argv else Path("var/demo")
    samples = Path(argv[1]) if len(argv) > 1 else Path("samples")
    drifted = differences(demo, samples)
    for name in drifted:
        print(f"{name} differs from samples/: regenerate or fix the change")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
