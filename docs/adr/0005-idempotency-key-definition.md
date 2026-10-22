# ADR-0005: Idempotency key = task + decision + target + state fingerprint

* Status: Accepted
* Date: 2026-10-21

## Context

"Same task/action/state" must be defined precisely to avoid both
duplicates and missed follow-ups.

## Decision

Fingerprint = sha256 of normalized status, due_date, owner, notes. Key =
sha256 of task_id, decision, target, fingerprint. Excluded: last_update,
message text, run id, policy version.

## Consequences

+ Reruns and cosmetic edits never re-notify; real changes do.
− An unchanged overdue task is reminded once; periodic re-reminders need an
explicit time bucket in the key (documented production follow-up).
