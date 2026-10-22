# OWASP LLM Prompt Injection Prevention Cheat Sheet — checklist

Scope: task notes are the only untrusted text that can reach a model;
the model classifies a note and may draft a message, it never acts.
Status words follow [`15`](../15-security-asvs-l2.md) §1. The note
interpreter and drafter are M5 work; rows that depend on them say so.

| Recommendation | Control here | Evidence | Status |
|----------------|--------------|----------|--------|
| Input validation and sanitisation | Notes are NFKC-normalised, stripped of control/format characters, capped and wrapped in `UntrustedText` at the trust boundary | `tests/unit/domain/test_normalize.py::test_notes_are_untrusted_cleaned_and_capped`, `test_full_width_text_is_nfkc_normalised` | Built |
| Detect injection attempts (including obfuscation) | `domain/injection.py` screens instruction-like phrases and role tags after normalisation; a flagged note never reaches the model (rule R2) | `tests/unit/domain/test_screen_and_prefilter.py`, `TestNeedsLlm::test_flagged_notes_never_reach_llm` | Built |
| Structured prompts with clear separation | Notes go into a delimited, escaped `<task_note>` block; instructions stay in the system role | — | Planned (M5 prompt builder) |
| Output validation | The model's interpretation must match a closed JSON schema and quote evidence from the note; drafts must pass `MessageValidator` (placeholders only, no links, dates, digits, names or state-change claims) | `tests/unit/domain/test_messages.py::test_violations`, property test 9 | Built for drafts; interpretation validation Planned (M5) |
| Human-in-the-loop / least privilege | No tools, no function calls, no state changes: the model returns a category; deterministic rules R1–R9 decide, and an injection-flagged note cannot widen a decision | `tests/property/test_domain_properties.py::test_8_llm_cannot_widen_the_decision` | Built |
| Remote content and RAG poisoning | The service fetches no remote content and has no retrieval store | — | N/A |
| Monitoring and logging | Screen hits and validation failures counted and audited without the note text | — | Planned (M6a logging) |
| Fail safe | Any model failure falls back to `UNCLEAR` with confidence 0 and template drafts | `tests/unit/domain/test_screen_and_prefilter.py::test_fallback_is_unclear_with_no_confidence` | Built |
