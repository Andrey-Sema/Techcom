# ADR-0002: PostgreSQL 18 for state and outbox; no Redis/Celery

* Status: Accepted
* Date: 2026-10-21

## Context

The brief accepts SQLite and says Redis/Celery are optional. We need
race-safe dedupe and safe concurrent dispatch to prove reliability.

## Decision

PostgreSQL 18 (Docker Compose, testcontainers in tests). `UNIQUE` +
`ON CONFLICT DO NOTHING` for dedupe; `FOR UPDATE SKIP LOCKED` + leases for
the outbox; native `uuidv7()` for ids. No message broker.

## Consequences

+ Production-parity semantics; one datastore; concurrency tests are real.
− Reviewers need Docker; mitigated by `make demo` and an in-memory
repository for fast unit tests.
Rejected: SQLite (no SKIP LOCKED, weak concurrency story), Redis/Celery
(extra infra without benefit at 10k/day).
