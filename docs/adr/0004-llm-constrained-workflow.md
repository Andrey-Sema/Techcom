# ADR-0004: LLM as a constrained classifier and drafter inside a workflow

* Status: Accepted; provider choice superseded by ADR-0013, drafting
  extended by the bounded orchestrator in ADR-0016
* Date: 2026-10-21

## Context

The LLM must not invent owners, dates or status; notes can contain prompt
injection; invalid output must be safe.

## Decision

Workflow, not agent: fixed code path, no tools. LLM tasks: (1) classify an
ambiguous note into a closed enum with verbatim evidence, (2) phrase a
message using placeholders. Strict local validation + grounding check;
any failure falls back deterministically. The real adapter is defined in
ADR-0013; a scripted stub is the default for tests.

## Consequences

+ Hallucinated facts cannot reach recipients; injection has no capability
to abuse.
+ Works fully offline; LLM improves coverage of messy notes.
− Less "agentic" than possible; this is intentional for a system that
messages real people.
