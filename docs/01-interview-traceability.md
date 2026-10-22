# 01 — Interview → Assessment → Product Traceability

The final assessment is a direct, executable version of the questions asked
in the first interview (Victor Kravets' nine technical questions). The product
must *demonstrate* the answers, and the live review will probe exactly these
points. This table links each question to the brief and to concrete product
features, and flags where the interview answer should be sharpened.

| # | Interview question | Brief section | Product feature that answers it | Talking point to sharpen |
|---|--------------------|---------------|----------------------------------|--------------------------|
| Q1 | AI vs ML vs LLM; multimodal models | §2B (where LLM fits) | LLM used *only* for language tasks; rules for everything computable | **AI** = field of systems performing tasks that need "intelligence"; **ML** = subset of AI that learns patterns from data (training/fine-tuning is one activity inside ML); **LLM** = a large transformer language model, one family of ML models. RAG is *not* training — it is retrieval-augmented prompting at inference time. |
| Q2 | What is an API and how to integrate AI through it | §2A, §5 | `TaskSource` / `Delivery` / `LLMClient` ports with HTTP adapters, typed contracts, retries | An API is a **contract** (operations, data formats, errors, auth) between systems; an API key is only one auth mechanism. REST = resource-oriented HTTP style; FastAPI is a framework for building such APIs. |
| Q3 | Prompt engineering, predictable answers | §2B, §4 S7 | Structured output with JSON Schema, closed enums, temperature 0, versioned prompts, few-shot examples, golden tests, validation + fallback | Predictability comes from **constraining the output space** (schema, enums) and **verifying** it in code — not from the prompt alone. |
| Q4 | AI agent vs chatbot | §1 | Deliberately a **workflow** (fixed code path with LLM steps), not an autonomous agent; LLM has zero tools | Agents choose their own steps/tools; workflows orchestrate LLM calls through predefined code paths. For a system that writes to production data, a workflow with least privilege is the safer default (Anthropic, *Building effective agents*). |
| Q5 | Agent that controls monday.com tasks and sends follow-ups | whole brief | Ingest → decide → draft → outbox → mock delivery; `TaskSource` adapter is swappable for monday.com GraphQL + webhooks | Show the adapter seam: replacing the fixture adapter with monday.com is a new adapter, zero domain change. |
| Q6 | Is "monday API + webhooks → Python backend → AI agent" acceptable? | §2D | Same shape, plus outbox + idempotent receiver | Add: webhooks are at-least-once and unordered → dedupe by event id, process idempotently, reconcile periodically with a full scan. |
| Q7 | Protecting against hallucinations / wrong actions | §2B, §4 S7–S8 | Capability removal (LLM cannot express owner/date/status), JSON Schema, grounding check (evidence must be a verbatim substring of the note), deterministic verifier, safe fallback, human-in-the-loop for escalations | A second LLM "judge" is optional; a **deterministic verifier** is cheaper, faster and cannot be prompt-injected. Use an LLM judge only for qualities code cannot check (tone). |
| Q8 | 10,000 AI tasks/day — fast, reliable, cheap | §8 review | Rules-first (LLM only for ambiguous notes), content-hash cache, small model tier, concurrency limits, circuit breaker, provider fallback, Batch API for non-urgent work, cost metrics | 10k/day ≈ 0.12 req/s average — throughput is trivial; the real problems are **cost, rate limits and correctness**. Do **not** propose rotating many free-tier keys to bypass quotas (violates provider terms); use a paid tier, quota increases, batching and caching. |
| Q9 | Timeout while changing a project status — avoid duplicates | §2D, §4 S6 | Stable operation identity (`Idempotency-Key` = UUIDv5 of the action key), outbox state `UNKNOWN`, reconciliation via `GET /messages/{operation_id}`, receiver dedupe | **A timeout means "outcome unknown", not "failed".** Never blindly retry a non-idempotent write. Retry with the *same* idempotency key (server replays the original result) or query the operation by id first (reconciliation). This was the weakest interview answer — the product must make it the strongest demo. |

## Context from the HR part

* TechCom: US telecom construction (fiber splicing, handholes, conduit,
  permits) — the fixture vocabulary matches. Use domain terms correctly in
  messages (OSP, as-built, splice verification).
* They already run: monday.com as source of truth (transition period), their
  own Python backend + DB, an AI note-taker that extracts "who promised what"
  and sends SMS/email confirmations. Our design is a clean, safer core of that
  same system.
* Candidate strengths to showcase: real production experience with Gemini
  multimodal parsing and human-in-the-middle validation (the 1C order-entry
  project) — reuse that story: "the LLM proposes, deterministic code and/or a
  human disposes".
