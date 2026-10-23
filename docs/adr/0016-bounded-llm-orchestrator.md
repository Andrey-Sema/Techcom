# ADR-0016: A bounded LLM orchestrator for follow-up actions

* Status: Accepted (extends ADR-0004 and ADR-0005)
* Date: 2026-10-23

## Context

ADR-0004 kept the model on a fixed path: classify a note, then pick one
of two code-written phrasings. That was safe but left the model almost
no judgement, while the role is about using an LLM as an orchestrator of
business automation. The brief still forbids invented owners, dates or
status, unsafe automatic actions and duplicate follow-ups.

## Decision

For every **new** actionable task state, a tool-using orchestrator plans
the action within limits that code sets and enforces:

1. **Policy first.** The deterministic rule table decides whether the
   task needs attention and computes a baseline action. Quiet tasks never
   reach the model, so it cannot create work.
2. **Envelope.** `domain/envelope.py` lists the alternatives the code
   accepts for the same facts: allowed decision kinds, recipients as
   opaque references (`owner`, `coordinator`, `manager`, `assigned_team`)
   and the minimum priority. There is never a `no_action` option.
   Escalating instead of following up is allowed only after an earlier
   action for the task was sent and the task is still overdue, so first
   contacts follow the brief's routing.
3. **Tools, read-only.** The model works in at most four JSON steps:
   `get_history` (earlier actions for the task), `check_plan` (dry-run of
   the guard, returns violation codes) and `submit_plan`. No tool sends,
   writes or reaches the network. The outbox remains the only executor.
4. **Guard.** `domain/guard.py` reviews every plan: envelope membership,
   priority only rises, message rules (facts only as placeholders that
   code renders; no digits, dates or relative deadlines, names or other
   proper nouns, links, addresses or phone numbers in any obfuscated
   form, no unverifiable or status claims, no instruction-like text, no
   markup, ASCII only, no copying of the note), a bounded rationale and
   evidence quoted verbatim from the note.
   Violations go back to the model once per step so it can repair.
5. **Fallback.** Any transport error, invalid JSON, unknown tool, wrong
   final step, breaker or budget refusal, or a plan that never passes the
   guard yields the baseline with its template and an `llm_fallback` flag.
6. **Dedupe slot.** The idempotency key is derived from the baseline,
   not from the model's choice (`DecisionWrite.baseline`). A rerun of an
   unchanged task finds the slot taken and makes no model call; model
   nondeterminism cannot produce a second message for one task state.
7. **Data minimisation.** The model sees status, dates, missing fields,
   the baseline reason and recipient *kinds*; personal names stay in code.
   If the note, title or project name trips the injection screen, all of
   them are withheld.
8. **Audit.** Each plan stores the baseline next to the chosen decision,
   a bounded rationale, and an `agent.plan` audit event with closed
   fields: tools used, step count, violation codes, outcome and whether
   the decision or recipient changed.

The offline model (`llm_agent_stub.py`) speaks the same protocol from the
code-owned context only, so the demo and tests exercise the full loop
without credentials. `FOLLOWUP_LLM_PLANNER=templates` restores the
previous fixed path.

## Consequences

+ The model makes real decisions (action, recipient, priority, wording,
  escalation of unanswered follow-ups) that are visible and explained.
+ Containment is proven by tests: a Hypothesis property feeds arbitrary
  and hostile model outputs through the loop and checks that every result
  is inside the envelope or is the baseline.
+ Duplicate safety does not depend on model determinism.
− Up to four model calls per new action instead of one; bounded by the
  step limit, per-call timeout, circuit breaker and daily budget.
− Placeholder-only facts constrain style (no literal dates or numbers).
− Quality of a real provider's choices is not yet measured on real notes.
