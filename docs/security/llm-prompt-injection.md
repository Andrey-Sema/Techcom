# OWASP LLM Prompt Injection Prevention Cheat Sheet — checklist

Scope: notes, titles and project labels are untrusted tracker text;
the model classifies a note and plans a follow-up inside a code-owned
envelope; it never acts directly.

The orchestrator chooses message *intents* (asks, tone, a mention of an
earlier message, hold for review) and code composes every word from
reviewed fragments (ADR-0018); no model text reaches a recipient.
Titles and project labels are omitted from model context. Suspicious
tracker fields withhold the note; contact data is redacted before
exposure. This is not guaranteed anonymisation. Evidence must occur in
both source and visible note, including cache hits. Only closed
violation codes are fed back to the model. Source-state slots prevent
interpretation recovery from creating another delivery (ADR-0017).
The full OWASP LLM Top 10 matrix is in
[`owasp-llm-top10.md`](owasp-llm-top10.md).
Status words follow [`15`](../15-security-asvs-l2.md) §1. The note
interpreter and drafter are in `application/interpreter.py`,
`application/drafter.py` and `application/llm_workflow.py`.

| Recommendation | Control here | Evidence | Status |
|----------------|--------------|----------|--------|
| Input validation and sanitisation | Notes are NFKC-normalised, stripped of control/format characters, capped and wrapped in `UntrustedText` at the trust boundary | `tests/unit/domain/test_normalize.py::test_notes_are_untrusted_cleaned_and_capped`, `test_full_width_text_is_nfkc_normalised` | Built |
| Detect injection attempts (including obfuscation) | `domain/injection.py` screens instruction-like phrases and role tags after normalisation; a flagged note never reaches the model (rule R2) | `tests/unit/domain/test_screen_and_prefilter.py`, `TestNeedsLlm::test_flagged_notes_never_reach_llm` | Built |
| Structured prompts with clear separation | Notes go into a delimited, HTML-escaped `<task_note>` block; instructions stay in the instructions field and name the element as untrusted | `tests/unit/llm/test_language_workflow.py::test_note_delimiter_is_escaped_without_fact_leaks`, `tests/scenarios/test_llm_safety.py::test_s8_prompt_injection_note_is_untrusted_data` | Built |
| Output validation | The model's interpretation must match a closed, bounded JSON schema and quote evidence found in the note (grounding), also when read back from the cache; drafts must pass `MessageValidator` (placeholders only, no links, dates, digits, names or state-change claims) | `tests/unit/llm/test_language_workflow.py::test_strict_output_bounds`, `test_cache_metadata_and_grounding_are_revalidated`, `tests/property/test_llm_output.py`, `tests/unit/domain/test_messages.py::test_violations` | Built |
| Human-in-the-loop / least privilege | Three read-only planning tools; no tool can send or mutate tasks. Rules and the envelope constrain choices; the outbox executes guarded actions | `tests/property/test_agent_containment.py` | Built |
| Bounded agency (orchestrator, ADR-0016, ADR-0018) | Read-only tools only (`get_history`, `check_plan`, `submit_plan`); choices limited to a policy envelope with opaque recipient references, allowed asks and tones; the plan has no message-text field and code composes every word; the only delivery power is *hold for review*, which a dispatcher never sends; four-step budget, per-call timeout and one total plan deadline; a flagged note, title or project withholds the note; title and project are never sent; contacts in notes are redacted; baseline fallback; dedupe slot owned by the policy | `tests/property/test_agent_containment.py`, `tests/unit/domain/test_guard.py`, `tests/unit/processing/test_orchestrator.py`, `tests/unit/processing/test_review_flow.py`, `tests/unit/llm/test_owasp_llm_top10.py` | Built |
| Remote content and RAG poisoning | The service fetches no remote content and has no retrieval store | — | N/A |
| Monitoring and logging | Closed JSON operational logs without note text (`infrastructure/log_config.py`); `followup_security_events_total{kind="note_flagged"}`, `followup_llm_requests_total{result}` and `followup_agent_plans_total{outcome}` with alerts for injection spikes and fallback rates | `tests/unit/cli/test_logging.py`, `tests/meta/test_observability_config.py` | Built |
| Fail safe | Any model failure falls back to `UNCLEAR` with confidence 0 and template drafts | `tests/unit/domain/test_screen_and_prefilter.py::test_fallback_is_unclear_with_no_confidence` | Built |
