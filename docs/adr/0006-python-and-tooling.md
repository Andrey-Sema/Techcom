# ADR-0006: Python ≥ 3.13, uv, ruff (79 cols), mypy strict

* Status: Accepted
* Date: 2026-10-21

## Context

The candidate asked for a modern stack (Python 3.13, PostgreSQL 18, uv)
and strict PEP 8.

## Decision

`requires-python = ">=3.13"`; CI matrix 3.13 + 3.14; Docker image 3.14;
3.15 (released 2026-10-09) as a non-blocking canary later. uv for
env/lock; ruff lint+format with line length 79; mypy --strict (ty is beta);
Pydantic v2; no pre-release dependencies.

## Consequences

+ Honors the requested baseline while running on a mature newer runtime.
− 79 columns is stricter than common 88; chosen deliberately for PEP 8.
