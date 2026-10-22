# 11 — Research Sources

Authoritative references consulted for the design (accessed 2026-10-21).
Primary sources first; secondary sources only where they corroborate.

## Reliability & distributed systems

* Stripe API — Idempotent requests:
  <https://docs.stripe.com/api/idempotent_requests> — client-generated keys,
  stored first response replayed on retry, parameter mismatch rejected,
  ≥ 24 h retention. Basis of the mock receiver semantics.
* IETF HTTPAPI WG — *The Idempotency-Key HTTP Header Field*
  (draft-ietf-httpapi-idempotency-key-header-07):
  <https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/>
  — header name/semantics; key must not be reused with a different payload.
* Chris Richardson — Pattern: Transactional outbox:
  <https://microservices.io/patterns/data/transactional-outbox> — dual-write
  problem, polling publisher, consumers must be idempotent.
* AWS Architecture Blog — Exponential Backoff and Jitter:
  <https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/>
  — full jitter recommendation.
* Google SRE Book — ch. 21 Handling Overload (client-side throttling) and
  ch. 22 Addressing Cascading Failures (retry in one layer, budgets):
  <https://sre.google/sre-book/handling-overload/>
* PostgreSQL docs — `SELECT … FOR UPDATE SKIP LOCKED`:
  <https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE>
* PostgreSQL 18 release announcement (async I/O, `uuidv7()`, virtual
  generated columns, OAuth, temporal constraints):
  <https://www.postgresql.org/about/news/postgresql-18-released-3142/>

## LLM safety & integration

* OWASP Top 10 for LLM Applications 2025 (LLM01 Prompt Injection, LLM05
  Improper Output Handling, LLM06 Excessive Agency):
  <https://genai.owasp.org/llm-top-10/>
* Google Gemini API — Structured output (`response_mime_type`,
  `response_schema` with Pydantic, `response.parsed`):
  <https://ai.google.dev/gemini-api/docs/structured-output>
* Google Gemini API — Models and pricing (pin a stable model id at
  implementation time): <https://ai.google.dev/gemini-api/docs/models>,
  <https://ai.google.dev/gemini-api/docs/pricing>
* Anthropic — Building effective agents (workflows vs agents):
  <https://www.anthropic.com/engineering/building-effective-agents>

## Security standards (added 2026-10-22)

* OWASP ASVS 5.0.0 (≈ 350 requirements, 17 chapters; read from the
  official repository `OWASP/ASVS`, folder `5.0/en`):
  <https://github.com/OWASP/ASVS/tree/master/5.0/en>
* OWASP Cheat Sheet Series (repository `OWASP/CheatSheetSeries`), notably
  FastAPI Security, LLM Prompt Injection Prevention, Docker Security,
  Logging, Logging Vocabulary, JSON Web Token, REST Security, Secrets
  Management, Webhook Security, Secure Coding with AI:
  <https://cheatsheetseries.owasp.org/>
* OWASP Cheat Sheet ↔ ASVS index: <https://cheatsheetseries.owasp.org/IndexASVS.html>

## Infrastructure & observability (added 2026-10-22)

* Docker official `postgres` image — PostgreSQL 18 moved `PGDATA` to
  `/var/lib/postgresql/18/docker`; mount the volume at `/var/lib/postgresql`:
  <https://github.com/docker-library/postgres/issues/1370>
* Grafana Loki — Promtail end of life (2026-03-02), use Grafana Alloy:
  <https://grafana.com/docs/loki/latest/send-data/promtail/>
* Grafana Alloy — collecting logs:
  <https://grafana.com/docs/alloy/latest/>
* uv — Docker integration guide: <https://docs.astral.sh/uv/guides/integration/docker/>

## Python tooling

* Python 3.15.0 release (2026-10-09) — reason to keep it as a CI canary, not
  the baseline: <https://www.python.org/downloads/release/python-3150/>
* PEP 8 — line length 79 / 72: <https://peps.python.org/pep-0008/>
* uv documentation (projects, lockfile, Docker integration):
  <https://docs.astral.sh/uv/>
* Ruff rules: <https://docs.astral.sh/ruff/rules/>
* ty status (beta in 2026 → mypy chosen):
  <https://pydevtools.com/blog/ty-beta/>
* stamina — retries with sane defaults, custom backoff via hook for
  `Retry-After`, testing mode: <https://stamina.hynek.me/en/latest/>
* SQLAlchemy 2.0 asyncio + psycopg 3 dialect:
  <https://docs.sqlalchemy.org/en/20/dialects/postgresql.html>
* Testcontainers for Python — getting started:
  <https://testcontainers.com/guides/getting-started-with-testcontainers-for-python/>
* Hypothesis — property-based testing: <https://hypothesis.readthedocs.io/>

## Notes on verification

* Gemini model ids and prices change frequently (2.5-series retirement was
  scheduled for October 2026 per third-party trackers); the model id is
  configuration and must be checked against the official model list on the
  day of implementation.
* Exact library versions are resolved by `uv lock` at bootstrap; no
  pre-release dependencies (e.g. SQLAlchemy 2.1 beta is excluded).
