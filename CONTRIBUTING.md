# Contributing

## Setup

```bash
make bootstrap      # uv sync (dev, gates, docs) + pre-commit hooks
make quality        # Ruff, mypy --strict, Bandit, import-linter
make test           # full suite, 100 % branch coverage (needs Docker)
make gates          # Semgrep rules, complexity, dead code, deps, audit,
                    # spelling, YAML, workflows, pyproject, docs
```

Python 3.13 and 3.14 are supported; `uv.lock` is the single source of
dependency versions. Never edit it by hand: run `uv lock`.

## Branches and pull requests

* One branch per milestone or fix (`feat/m4-use-cases`,
  `fix/receiver-race`); `main` only changes through pull requests.
* Commit messages follow Conventional Commits (`feat:`, `fix:`, `test:`,
  `docs:`, `build:`, `ci:`); the `commit-msg` hook checks them.
* Fill in the pull request template, including the gates you ran and
  the ASVS requirements the change touches.

## Rules the gates enforce

* PEP 8 at 79 columns (Ruff `ALL`), docstrings at 72.
* Comments and docstrings in English only
  (`tests/meta/test_english_only.py`).
* No `pragma: no cover`, no bare `# type: ignore`, no bare `# nosec`.
* Project invariants in `.semgrep/project-invariants.yml`: no
  `str.format` on message templates, no `verify=False`, no redirects,
  no system clock in the core, no SQL built from strings, no task notes
  or prompts in logs, no environment access outside settings. A new
  rule needs a positive and a negative case in
  `.semgrep/project-invariants.py`.

## Definition of done

See `docs/07-acceptance-criteria.md` section 8. In short: a failing-first
test, all gates green, docs and the progress tracker in
`docs/08-implementation-plan.md` updated.
