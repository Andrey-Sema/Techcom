# ADR 0015: bounded writes and durable admission

Status: implemented; execution evidence pending.

## Context

The measured 10,000-task run took 83 seconds and its rerun took 71 seconds.
Each task waited for its own decision, action, outcome and audit writes.
Retention also removed the only duplicate identity when deleting an old
terminal action. Process-local API limits did not coordinate replicas.

## Decision

Prepare tasks sequentially and persist batches with eight concurrent
transactions by default, configurable from one to sixteen. Use `TaskGroup`
to join or cancel every writer before finishing the run. Treat a cancelled
child as failure. Keep per-decision atomicity; a failed run may contain
already committed decisions, whose replay remains idempotent.

Read existing message data only for the current batch of identities, using
indexed ledger/action joins. A retained identity without payload skips
drafting and uses a local template for its duplicate receipt; it cannot
create another action. Run reports select only their own actions. Neither
path materializes the complete historical outbox.

Persist a source position independently of database-generated UUIDs. Reports
order by that position. Bind source project directly to the normalized
task, including quiet decisions, and check it independently of the action
target. Historical rows without provenance remain unavailable to scoped
callers. Never infer source authority from an LLM response or message text.
Reject noncanonical project names instead of applying lossy display cleanup
to an authorization identifier. Missing-project placeholders have no scoped
authority.

Use a data-modifying PostgreSQL CTE to combine the existing decision's
outcome update and audit insert. The decision insert remains an earlier
statement: updating a newly inserted row in the same statement would not
have the required PostgreSQL snapshot semantics.

Migration `0004` backfills an immutable `action_keys` ledger under the
actions table lock. A fixed-search-path, owner-executed insertion trigger
reserves identities in the same transaction as the action. A duplicate
reservation suppresses the insert, including after payload retention.
Neither service nor maintenance roles can erase the ledger. Downgrade
refuses to discard an identity whose payload has already been pruned.

API replicas use bounded fixed windows in PostgreSQL, with database time,
separate successful/failed-auth namespaces and a cardinality bound protected
by a transaction advisory lock. Store hashed subjects only. No worker-local
fallback is allowed when the shared store fails. All replicas must use the
same admission configuration. A gateway still owns connection/header/CPU
ingress limits and trusted client-address handling.
Admission functions are strict on null arguments and require READ COMMITTED
so statements after a transaction lock see the previous holder's commits.

Before a send, reserve a recipient/day slot under an owned action lease and
a recipient/day advisory lock. PostgreSQL time determines the UTC day,
independently of replica clock skew. Repeating one operation on the same day
does not spend another slot. Reservations are conservative: uncertain sends
are never refunded. At capacity, defer to the next UTC day without spending
a delivery attempt. Identity is target kind plus trimmed, lowercased target
name, independent of project; aliases are not resolved into a person.

Terminal retention starts at `terminal_at`; legacy DEAD rows conservatively
start at migration time. Run and decision retention requires an old
`finished_at`. Running or recently finished old runs survive. Recipient
admission history follows action retention, with a database-enforced minimum
of two days. Permanent duplicate identities contain no message payload.
Abandoned runs require explicit operational reconciliation; age alone does
not prove that a running run can safely be deleted.

## Consequences and evidence boundary

Memory and PostgreSQL implementations retain the same domain semantics.
Only the persistent runtime enforces deployment-wide recipient limits;
the memory runtime is explicitly for disposable demos/tests.

The benchmark gates now require first run below 60 seconds, rerun below
30 seconds and RSS below 300 MB on the documented 10,000-task workload.
No new timing is asserted: the user prohibited environment verification.
New race, retention, authorization and Hypothesis tests are authored but
unexecuted. The earlier 83/71-second results remain historical evidence.
