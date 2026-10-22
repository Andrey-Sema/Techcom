# ADR-0013: OpenAI model router (Responses API, Sol generation)

* Status: Accepted
* Date: 2026-10-22
* Supersedes: the provider choice in ADR-0004 (Gemini as the only real
  integration). The safety design of ADR-0004 is unchanged.

## Context

The candidate wants the real LLM integration on OpenAI with the current
Sol generation (`gpt-5.6-sol`, `gpt-6-sol`, `gpt-6.1-sol`) and a router,
as discussed in the interview ("if one backend fails, switch to another
model"). Verified on 2026-10-22: `openai` 3.36.1 on PyPI lists these model
ids, reasoning efforts `none … max`, `responses.create/parse`, and
workload-identity authentication.

## Decision

* `LLMRouter` implements the `LLMClient` port. Per purpose it walks an
  ordered route list from `config/llm.toml`:
  `gpt-6.1-sol` → `gpt-6-sol` → `gpt-5.6-sol` (different generations, so
  one deployment incident rarely takes down all three).
* Failover only on availability failures (timeouts, connection errors,
  429, 409, 5xx, model not found / not permitted / rejected request).
  Refusals and incomplete outputs are returned to the caller, which falls
  back deterministically; credentials errors stop immediately.
* One circuit breaker per model; one daily spend budget (USD) computed
  from provider-reported usage and a validated price table (cached
  input, cache-write premium, long-context multipliers; unknown usage is
  billed conservatively).
* `OpenAIResponsesBackend` uses the Responses API with a strict JSON
  schema, `reasoning.effort = "low"`, `verbosity = "low"`,
  `store = False`, `truncation = "disabled"`, `prompt_cache_key =
  purpose:prompt_version`, per-request timeout, and **SDK retries
  disabled** (`max_retries = 0`) — retries live in one layer.
* Credentials: `api_key` mode reads the key from a Docker secret file
  (`followup_openai_api_key`) into `SecretStr`, validates its format and
  never echoes it (`hide_input_in_errors`); `workload_identity` mode uses
  the SDK's token exchange (Kubernetes service-account token), so
  production has no static key (ASVS 13.2.1). `base_url` is pinned and
  allow-listed; `OPENAI_BASE_URL` from the environment is ignored.
* The model catalogue is validated against the installed SDK by a test,
  so a typo or an SDK downgrade fails CI.

## Consequences

+ Real provider integration with cross-generation failover, cost control
  and a no-static-key production path.
+ Gemini (or any provider) becomes another backend behind the same
  router; the application layer does not change.
− Sol is the flagship tier; for pure classification a cheaper tier
  (e.g. `gpt-6-luna`) can be added as the first route of
  `interpret_note` once the eval harness shows equal quality.
− Prices and model details change quickly: `verified_on` in the config
  and the live smoke test (`pytest -m live_llm`) are the guard.
