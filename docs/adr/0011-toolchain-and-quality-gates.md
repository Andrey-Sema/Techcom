# ADR-0011: Toolchain and quality gates (Ruff ALL + Bandit + Semgrep)

* Status: Accepted
* Date: 2026-10-22

## Context

A design audit found quality claims without enforcing tools
(complexity, dead code, dependency hygiene, project invariants). The
candidate explicitly requires Ruff and Bandit.

## Decision

Ruff with `select = ["ALL"]` and a short, commented ignore list (79 cols,
Google docstrings, C90 ≤ 8) as linter and formatter; **standalone Bandit**
as the authoritative Python SAST gate (Ruff `S` stays for instant
feedback); Semgrep with registry packs and custom rules that encode design
invariants; mypy strict; import-linter; xenon; vulture; deptry;
codespell; yamllint; hadolint; zizmor; gitleaks; pip-audit. One
`pre-commit run --all-files` is the same gate locally and in CI. All
tools and actions pinned (versions, SHAs, digests).

## Consequences

+ Every README claim maps to a failing gate.
+ Design invariants (no `str.format` on drafts, no `verify=False`, no
naive clock in core) cannot regress silently.
− More configuration to maintain; mitigated by the `Makefile` as the
single entry point and Dependabot with cooldown.
