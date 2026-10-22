"""Fail when the mutation score drops below the docs/06 target.

Reads the stats that ``mutmut export-cicd-stats`` writes. A timeout
counts as killed: the mutant made the tests hang, so they noticed it.
"""

import json
import sys
from pathlib import Path

STATS = Path("mutants/mutmut-cicd-stats.json")
MIN_SCORE = 0.90


def main() -> int:
    """Print the score and return a non-zero exit code below target."""
    stats = json.loads(STATS.read_text(encoding="utf-8"))
    killed = stats["killed"] + stats["timeout"]
    total = stats["total"]
    score = killed / total if total else 0.0
    print(
        f"mutation score {killed}/{total} = {score:.1%} "
        f"(minimum {MIN_SCORE:.0%}); survivors: {stats['survived']}"
    )
    return 0 if score >= MIN_SCORE else 1


if __name__ == "__main__":
    sys.exit(main())
