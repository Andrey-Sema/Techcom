# ADR-0003: Transactional outbox, idempotent receiver, reconciliation

* Status: Accepted
* Date: 2026-10-21

## Context

Requirement D: no duplicate follow-ups; handle a timeout where the write
may have succeeded.

## Decision

Decision and action rows are written in one transaction; a dispatcher
sends later with `Idempotency-Key = operation_id` (UUIDv5 of the action key).
Read timeouts on writes move the action to `unknown`; a reconciler looks the
operation up by id before any resend. The mock receiver replays stored
responses for repeated keys and rejects key reuse with a different payload.

## Consequences

+ Effectively-once delivery with two independent safety nets.
+ No network I/O inside DB transactions.
− Slight delivery latency (dispatch step); acceptable for follow-ups.
