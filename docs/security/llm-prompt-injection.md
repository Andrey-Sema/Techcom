# OWASP LLM Prompt Injection Prevention Cheat Sheet — checklist

Scope: task notes are the only untrusted text that can reach a model;
the model classifies a note and may draft a message, it never acts.
Status words follow [`15`](../15-security-asvs-l2.md) §1. The note
interpreter and drafter are in `application/interpreter.py`,
`application/drafter.py` and `application/llm_workflow.py` (M5).

| Recommendation | Control here | Evidence | Status |
|----------------|--------------|----------|--------|
| Input validation and sanitisation | Notes are NFKC-normalised, stripped of control/format characters, capped and wrapped in `UntrustedText` at the trust boundary | `tests/unit/domain/test_normalize.py::test_notes_are_untrusted_cleaned_and_capped`, `test_full_width_text_is_nfkc_normalised` | Built |
| Detect injection attempts (including obfuscation) | `domain/injection.py` screens instruction-like phrases and role tags after normalisation; a flagged note never reaches the model (rule R2) | `tests/unit/domain/test_screen_and_prefilter.py`, `TestNeedsLlm::test_flagged_notes_never_reach_llm` | Built |
| Structured prompts with clear separation | Notes go into a delimited, HTML-escaped `<task_note>` block; instructions stay in the instructions field and name the element as untrusted | `tests/unit/llm/test_language_workflow.py::test_note_delimiter_is_escaped_without_fact_leaks`, `tests/scenarios/test_llm_safety.py::test_s8_prompt_injection_note_is_untrusted_data` | Built |
| Output validation | The model's interpretation must match a closed, bounded JSON schema and quote evidence found in the note (grounding), also when read back from the cache; drafts must pass `MessageValidator` (placeholders only, no links, dates, digits, names or state-change claims) | `tests/unit/llm/test_language_workflow.py::test_strict_output_bounds`, `test_cache_metadata_and_grounding_are_revalidated`, `tests/property/test_llm_output.py`, `tests/unit/domain/test_messages.py::test_violations` | Built |
| Human-in-the-loop / least privilege | No tools, no function calls, no state changes: the model returns a category; deterministic rules R1–R9 decide, and an injection-flagged note cannot widen a decision | `tests/property/test_domain_properties.py::test_8_llm_cannot_widen_the_decision` | Built |
| Remote content and RAG poisoning | The service fetches no remote content and has no retrieval store | — | N/A |
| Monitoring and logging | Closed JSON operational logs without note text (`infrastructure/log_config.py`); metrics for screen hits and validation failures arrive with the metrics endpoint (M6b) | `tests/unit/cli/test_logging.py` | Built for logs; metrics Planned (M6b) |
| Fail safe | Any model failure falls back to `UNCLEAR` with confidence 0 and template drafts | `tests/unit/domain/test_screen_and_prefilter.py::test_fallback_is_unclear_with_no_confidence` | Built |
