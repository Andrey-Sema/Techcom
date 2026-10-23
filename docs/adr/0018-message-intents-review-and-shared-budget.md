# ADR-0018: Message intents, human review and a shared LLM budget

* Status: Accepted (supersedes the message wording of ADR-0017;
  extends ADR-0016)
* Date: 2026-10-23

## Context

ADR-0017 closed the free-prose channel by letting the model pick one of
two reviewed subject/body pairs per action, which differed only by a
closing phrase. That was safe but left the orchestrator almost nothing
to decide about the message, and it made the model copy text verbatim,
which real models do imperfectly (quotes, spacing) at the cost of
repair steps. The role asks for an LLM that orchestrates the follow-up.

Three related gaps remained: the model could not ask for a person when
the evidence conflicted; its rationale was discarded; and the daily
LLM budget only covered one process (OWASP LLM10).

## Decision

1. **Intents, not text.** The plan schema has no field that reaches a
   recipient. The model chooses:
   * `asks`: one or two of status, blocker, unblock plan, delivery ETA,
     missing details, owner, completion date, on-track check. Allowed
     asks depend on the decision kind and the task (the owner question
     only for an unowned task); each kind has required asks (an ETA
     request must ask for the delivery date).
   * `tone`: neutral, friendly or firm. Firm only after an unanswered,
     overdue message or for an escalation.
   * `earlier_contact`: mention an earlier message, only if one was sent
     (LLM09: "we wrote before" must be a verified fact).

   `domain/agent_messages.py` composes the message from reviewed
   fragments in a fixed order; code renders every fact. Every
   composable message is tested against the fact rules and the
   receiver's length limits with 80-character facts, and the guard
   re-checks the fragments at run time.
2. **Hold for review.** `delivery: hold_for_review` with a closed
   `hold_reason` stores the action as `held` (migration 0007). No
   dispatcher claims it; `followup review --approve|--reject` moves it to
   `pending` (same operation identity) or `dead`, with an audit event.
   Holding can only reduce automation (LLM06). Abuse (notes crafted to
   make the model hold everything) delays mail and is caught by the
   held-backlog alert; `FOLLOWUP_LLM_PLANNER=templates` is the lever.
3. **The rationale is kept as an opinion.** The guard rejects a
   rationale with links, contacts, instruction-like text or a six-word
   run of the note (raw notes are never stored); the accepted rationale
   is stored in the plan audit as `model_rationale`, never rendered.
   The decision's own rationale stays code-owned.
4. **One total plan deadline** (60 s) separate from the per-call timeout
   (20 s): a four-step plan fits, a slow provider cannot stall a run.
5. **A shared spend ledger** (migration 0008). Before every provider
   call the router reserves the worst-case cost in the process budget
   and in `reserve_llm_spend`, one conditional `UPDATE` on the UTC day's
   row; `settle_llm_spend` charges the actual cost, or the bound when
   the bill is unknown. Overruns close admission. An unreachable ledger
   admits nothing. Leaked reservations stay charged to their day.
6. **Saved runs need PostgreSQL.** `followup run` without a database
   accepts only `--dry-run`; `followup demo` keeps the in-memory
   walkthrough. A second process can no longer re-enqueue work the
   first one sent.

## Consequences

+ The model makes visible, meaningful choices about each message, and
  can ask for a human when unsure; containment is unchanged: no model
  text reaches a recipient.
+ No exact-copy step: fewer repair rounds and wasted calls.
+ The budget holds across workers and restarts.
− Messages are less varied than free prose.
− `held` cannot be removed from the PostgreSQL enum on downgrade; the
  downgrade refuses while held actions exist.
− Review is a CLI; a UI with role-based approval is product work.
