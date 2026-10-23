# ADR-0017: Reviewed wording and source-state notification slots

* Status: Accepted (narrows ADR-0016)
* Date: 2026-10-23

## Problem

Name/date/link regexes accepted fabricated facts and unrecognised bare
domains. Separately, interpretation failure followed by recovery changed
the baseline and operation identity, notifying an unchanged task again.

## Decision

Keep model choice of permitted action, recipient and priority. Offer
reviewed message pairs in `domain/agent_messages.py`; the guard requires
exact membership for the chosen action. The model selects wording but
cannot insert prose. Code renders facts. Unapproved pairs trigger repair
then baseline fallback. Saved rationales describe the bounded choice
using code-owned text rather than treating model explanations as facts.

Keep operation IDs and receiver reconciliation. Additionally allow one
notification per `(task_id, source_project, state_fingerprint)`, independent
of model output. Memory enforces this under its write lock. Migration
0006 extends the existing PostgreSQL trigger, including older writers.
Slots and operation-key aliases survive payload pruning. Real source
changes permit new notifications; unchanged reminder cadence remains a
separate product decision.

Backfill from surviving decision receipts without rewriting operation
IDs. If pre-upgrade retention left opaque keys without source receipts,
refuse migration: hashes cannot recover task/project/state. Restore the
receipt inventory or prepare a separately reviewed recovery plan. Back
up before migration; stop workers during deployment and validate it on
PostgreSQL before production use. Downgrade requires an empty slot table.

Omit title/project values from planning context. Screen all tracker text
surfaces. Withhold the note when any is suspicious, and withhold suspect
display facts during rendering. Redact contacts before exposing notes.
This minimises contact data, not all personal names. Evidence must occur
in both source and visible redacted text, including cache hits.

Defer negation and non-unknown ETA references to interpretation. Honor
`needs_eta=False`; use a dedicated progress message without falsely
calling future work overdue.

## Verification and remaining OWASP LLM risks

Regression tests cover fabricated facts/status, unusual TLDs, tracker
prompt surfaces, contacts, known ETA, Unicode negation, actual gateway
sends after model recovery and legitimate offline planning. Repository
contracts cover races, pruning, project separation and real state changes.

The `hardening.v1` interpreter/drafter replay preserves independent labels
and documents changed scripts. Historical bundles remain archived.
Replay and agent contract tests do not measure live provider reasoning:
provider quality remains UNVERIFIED.

LLM01/02/05/06/09 controls are strengthened. LLM03 requires actual locked
dependency/image scans. No local training or vector store exists
(LLM04/08). Prompt secrecy is not an authorization boundary (LLM07).
Existing LLM10 controls bound steps, tokens, deadlines, concurrency and
per-process spend. A durable shared spend ledger remains unimplemented:
use one LLM worker and a provider-enforced spend ceiling until it exists.
`store=False` does not establish all provider retention terms.

Reviewed wording cannot prove tracker data or semantic classifications
true. Consequential real delivery still needs a product approval policy
and measured provider accuracy. This is not a claim of full ASVS L2 or
general prompt-injection immunity.
