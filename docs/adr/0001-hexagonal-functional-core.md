# ADR-0001: Hexagonal architecture with a functional core

* Status: Accepted
* Date: 2026-10-21

## Context

A small service must show clear modules, swappable integrations (fixture
→ monday.com), and deterministic tests for failure scenarios within a short
timebox.

## Decision

Ports & Adapters. Domain (normalize, policy, identity, messages) is pure
and synchronous; application use cases orchestrate ports; adapters implement
HTTP, LLM, PostgreSQL, clock. Manual composition root, no DI framework.
`import-linter` enforces the dependency direction.

## Consequences

+ Every external failure is injectable; policy is exhaustively testable.
+ monday.com adapter is additive.
− Slightly more files than a script; mitigated by keeping each module small.
Rejected: DDD/CQRS/event sourcing (overkill), single script (untestable).
