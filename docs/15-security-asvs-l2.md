# 15 — Security Baseline: OWASP ASVS 5.0 Level 2 + OWASP Cheat Sheets

> Implementation status: this document specifies the design target.
> Planned controls require implementation evidence before promotion;
> the verified M0/M2/M3 subset is recorded in
> [the foundation review](19-foundation-review.md).

## 1. Target and rules of the game

* **Wording:** "ASVS 5.0 L2 **design target**, verified items listed in
  `docs/asvs-traceability.md`, local deviations in §4" — never a bare
  "L2 compliant" (`16` #18).
* **Target:** OWASP ASVS **5.0.0, Level 2** (L2 includes all L1
  requirements) for the `techcom-followup` service: REST API, CLI, workers,
  persistence and outbound integrations. The mock API is a test double and
  out of scope, but it is still hardened and never shipped (15.2.3).
* **ASVS does not cover LLM-specific risks**, so it is complemented by the
  **OWASP Top 10 for LLM Applications 2025** (`04` §7) and the OWASP
  *LLM Prompt Injection Prevention* and *Secure AI Model Ops* cheat sheets.
* Every applicable requirement gets exactly one status:

| Status | Meaning |
|--------|---------|
| **Planned** | Specified by the design; implementation evidence is still required. |
| **Built** | Implemented and linked to an actual test or CI check. |
| **Prod** | Satisfied by the documented production configuration (TLS terminator, secret manager, IdP); local compose deviates on purpose. |
| **Doc** | A documentation requirement, satisfied by the referenced document. |
| **Deviation** | Not met; the risk is accepted in writing with a reason and an owner. |
| **N/A** | Feature does not exist in this system (reason given). |

Definition of done for security: **zero applicable L1/L2 requirements
without a status**, and every *Built* item links to a verification. Tests
carry a marker, e.g. `@pytest.mark.asvs("9.2.3")`; a CI step collects the
markers and renders `docs/asvs-traceability.md` (requirement → tests), so
the claim "L2" is evidence-backed, not aspirational.

## 2. Security decisions this baseline forces (changes to the earlier plan)

| # | Decision | ASVS driver |
|---|----------|-------------|
| D1 | REST API is protected by **short-lived signed service tokens** (JWT, `EdDSA`/Ed25519, `exp ≤ 15 min`, `aud`, `iss`, `typ=at+jwt`, `jti`, `scope`) instead of a static bearer key. Locally `scripts/issue_token.py` (not part of the shipped package) signs with a dev key; in production tokens come from the IdP (OAuth 2.0 client credentials / workload identity federation). | 13.2.1 forbids unchanging credentials such as API keys between backend components; V9 |
| D2 | **Deny-by-default authorization**: router-level dependency requires a valid token; each route declares its scope (`runs:trigger`, `runs:read`, `actions:read`). | 8.1.1, 8.2.1, 8.3.1 |
| D3 | **Two DB roles**: `followup_migrator` (DDL) and `followup_app` (DML only; `audit_events` INSERT/SELECT only). Production: short-lived DB credentials from a secret manager (dynamic secrets) or cloud IAM auth. | 13.2.1, 13.2.2, 16.4.2 |
| D4 | **Outbound allow-list**: the HTTP client factory accepts only hosts from configuration (task API, delivery API, LLM endpoint); `follow_redirects=False`; certificate verification can't be disabled. | 1.3.6, 13.2.4, 13.2.5, 15.3.2, 12.3.2 |
| D5 | **Placeholder rendering by regex, never `str.format`**: drafts are rendered with `re.sub(r"\{([a-z_]+)\}", …)` over an allow-listed dict — `{recipient_name.__class__}` cannot be evaluated. Subject lines are stripped of CR/LF. | 1.3.7, 1.3.10, 1.3.11 |
| D6 | **Per-recipient daily message cap** (config, e.g. 5/day) on top of dedupe — a business-logic limit against notification flooding even if state flaps. Excess actions are deferred to the next day (`next_attempt_at`) with a `security.rate_limited` event and an alert — never silently dropped. | 2.1.3, 2.3.2, 2.4.1 |
| D7 | **API rate limiting** per token subject (token bucket, in-process; Redis/gateway in prod) + body size limit (64 KiB) + run-trigger concurrency = 1. | 2.4.1, 15.2.2 |
| D8 | **Production surface minimised**: OpenAPI UI off in prod, metrics on a separate internal port, debug off, mock package not in the image. | 13.4.2, 13.4.5, 15.2.3 |
| D9 | **Security headers on every API response**: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, HSTS in prod. CORS disabled (no browser client). | 3.4.x, 14.3.2 |
| D10 | **Errors**: RFC 9457 `problem+json`, generic text + `request_id`; no stack traces, SQL or validation internals echoed. | 16.5.1, FastAPI cheat sheet |
| D11 | **LLM data handling**: only note text + task title leave the system; use a paid/enterprise tier whose terms exclude training on submitted data (free tiers may use content to improve products — verify current provider terms); production: Vertex AI with workload identity (no static API key). | 14.2.3, 13.2.1 |
| D12 | **Logs via OTLP, no Docker socket in any container.** | OWASP Docker Security rule #1, 16.4.3 |

## 3. Chapter applicability

| Chapter | Applies? | Why |
|---------|----------|-----|
| V1 Encoding & Sanitization | Yes | SQL, URLs, JSON, LLM prompts, message templates, log lines |
| V2 Validation & Business Logic | Yes | Core of the product |
| V3 Web Frontend Security | Partly | No browser UI; response headers still set (defence in depth) |
| V4 API & Web Service | Yes | REST API |
| V5 File Handling | N/A | No uploads; fixture path is operator config, read by the mock only |
| V6 Authentication | Partly | No human logins in the app; token validation and auth documentation apply; Grafana humans → IdP + MFA in prod |
| V7 Session Management | N/A | Stateless bearer tokens, no sessions/cookies |
| V8 Authorization | Yes | Scopes per route |
| V9 Self-contained Tokens | Yes | JWT validation |
| V10 OAuth & OIDC | Partly (prod) | Service is an OAuth *resource server* in production; no client/AS role |
| V11 Cryptography | Yes (light) | Hashing, signatures, CSPRNG; no app-level encryption |
| V12 Secure Communication | Yes (prod) | TLS at the edge and internally |
| V13 Configuration | Yes | Secrets, least privilege, hardening |
| V14 Data Protection | Yes | Classification, LLM data minimisation, caching |
| V15 Secure Coding & Architecture | Yes | Dependencies, SBOM, mass assignment, DoS |
| V16 Security Logging & Error Handling | Yes | See `14-logging-and-observability.md` |
| V17 WebRTC | N/A | Not used |

## 4. Requirement checklist (L1 + L2, applicable chapters)

### V1 Encoding and Sanitization

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 1.1.1 | 2 | Normalizer canonicalizes once (NFKC, whitespace) at the trust boundary; no later re-decoding | unit tests | Planned |
| 1.1.2 | 2 | Encoding at the last step: JSON by serializer, SQL by driver binds, prompt delimiters escaped when the prompt is built | unit tests | Planned |
| 1.2.1 | 1 | Only JSON responses via framework serializer | API tests | Planned |
| 1.2.2 | 1 | URLs built with `httpx` params / validated UUID path segments, never string concatenation of untrusted data | adapter tests | Planned |
| 1.2.3 | 1 | JSON produced only by `json`/Pydantic serializers | tests | Planned |
| 1.2.4 | 1 | SQLAlchemy Core with bound parameters; ruff `S608` | CI lint, integration tests | Planned |
| 1.2.5 | 1 | No OS command execution; ruff `S602–S607` | CI lint | Planned |
| 1.2.6–1.2.8 | 2 | No LDAP/XPath/LaTeX | — | N/A |
| 1.2.9 | 2 | Any dynamic regex input goes through `re.escape` (routing names, synonyms) | unit test | Planned |
| 1.3.1 | 1 | No HTML input | — | N/A |
| 1.3.2 | 1 | No `eval`/`exec`/dynamic import of untrusted names; ruff `S307`, `S102` | CI lint | Planned |
| 1.3.3 | 2 | Untrusted notes wrapped in `UntrustedText`; prompt tags escaped; control chars stripped | unit tests | Planned |
| 1.3.4, 1.3.5 | 2 | No SVG/Markdown/template-language rendering (messages are plain text) | — | N/A |
| 1.3.6 | 2 | No user-supplied URLs; outbound allow-list (D4) | adapter tests | Planned |
| 1.3.7 | 2 | Templates are code constants; regex placeholder renderer (D5) | unit + property tests | Planned |
| 1.3.8, 1.3.9 | 2 | No JNDI/memcache | — | N/A |
| 1.3.10 | 2 | No `str.format`/`%` on untrusted or LLM text (D5) | test with `{x.__class__}` payload | Planned |
| 1.3.11 | 2 | Message subject/body CR/LF and header-like content stripped before delivery (future SMTP safe) | unit test | Planned |
| 1.4.1–1.4.3 | 2 | Memory-safe language; numeric ranges bounded by Pydantic `Field` constraints | unit tests | Planned / N/A |
| 1.5.1 | 1 | No XML parsing (if ever: `defusedxml`) | — | N/A |
| 1.5.2 | 2 | Only JSON deserialization into strict models; no `pickle`/`yaml.load`; ruff `S301`, `S506` | CI lint | Planned |

### V2 Validation and Business Logic

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 2.1.1 | 1 | Validation rules documented (`04` §1) and published as JSON Schema | doc + schema test | Doc |
| 2.1.2 | 2 | Cross-field rules documented: `Done` vs notes, `last_update` in future, duplicate ids | doc | Doc |
| 2.1.3 | 2 | Limits documented: max tasks/run (10,000), max note length, per-recipient daily cap, LLM daily budget, retry budget | doc (`04`, `05`) | Doc |
| 2.2.1 | 1 | Positive validation: enums, regex for ids, ranges, `extra="forbid"` | unit + property tests | Planned |
| 2.2.2 | 1 | All validation server-side (no client) | — | Planned |
| 2.2.3 | 2 | Combination rules enforced in normalizer (flags, rejection) | unit tests | Planned |
| 2.3.1 | 1 | Outbox state machine forbids skipped/illegal transitions | state-machine tests | Planned |
| 2.3.2 | 2 | Limits from 2.1.3 enforced (cap, budget, max size) | tests per limit | Planned |
| 2.3.3 | 2 | Decision + action written in one DB transaction | integration test (fault between inserts) | Planned |
| 2.3.4 | 2 | `UNIQUE(idempotency_key)`, `SKIP LOCKED` claims — no double send | concurrency tests | Planned |
| 2.4.1 | 2 | API rate limit, LLM budget + breaker, retry budget, per-recipient cap (D6, D7) | tests | Planned |

### V3 Web Frontend Security (headers only)

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 3.2.1 | 1 | Correct `Content-Type`, `nosniff`, CSP `default-src 'none'` so API output is never rendered as a document | API header test | Planned |
| 3.4.1 | 1 | HSTS on all responses in prod (`max-age=31536000; includeSubDomains`) | config test (`ENV=prod`) | Planned |
| 3.4.2 | 1 | No CORS middleware → no `Access-Control-Allow-Origin` | API test | Planned |
| 3.4.3, 3.4.6 | 2 | CSP `default-src 'none'; frame-ancestors 'none'` | API test | Planned |
| 3.4.4 | 2 | `X-Content-Type-Options: nosniff` | API test | Planned |
| 3.4.5 | 2 | `Referrer-Policy: no-referrer` | API test | Planned |
| 3.5.3 | 1 | State-changing routes are `POST` only | route test | Planned |
| 3.3.x, 3.5.1–3.5.2, 3.5.4–3.5.5, 3.7.x, 3.2.2 | 1–2 | No cookies, no browser client, no redirects, no postMessage | — | N/A |

### V4 API and Web Service

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 4.1.1 | 1 | `application/json` / `application/problem+json` on every body (JSON has no charset parameter per RFC 8259; UTF-8 implied) | API test | Planned |
| 4.1.2 | 2 | API never redirects HTTP→HTTPS; plain HTTP is refused at the edge | prod config | Prod |
| 4.1.3 | 2 | `X-Forwarded-*` honoured only from configured proxy IPs (`--forwarded-allow-ips`) | config test | Planned |
| 4.2.1 | 2 | Single HTTP/1.1 parser behind one proxy; proxy normalises `Content-Length`/`Transfer-Encoding` | prod config | Prod |
| 4.3.x | 2 | No GraphQL server (monday.com GraphQL is consumed, not exposed) | — | N/A |
| 4.4.x | 1–2 | No WebSockets | — | N/A |

### V6 Authentication (applicable parts)

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 6.1.1 | 1 | Auth anti-automation documented: per-source rate limit on 401s, alert `FollowupAuthFailures` | doc + test | Doc/Planned |
| 6.1.3, 6.3.4 | 2 | One documented authentication pathway (service JWT); no hidden bypass (health/readiness are the only public routes, they return no data) | route inventory test | Planned |
| 6.3.1 | 1 | Failed-auth rate limiting | API test | Planned |
| 6.3.2 | 1 | No default accounts: DB superuser password generated; Grafana admin from secret, sign-up and anonymous disabled | config check | Planned |
| 6.3.3 | 2 | Humans (Grafana, ops) authenticate via company IdP with MFA in prod; services use workload identity | prod config | Prod |
| 6.8.2 | 2 | Token signatures always verified; unsigned/`none` rejected | unit tests | Planned |
| 6.8.4 | 2 | N/A — no step-up auth requirements | — | N/A |
| 6.2.x, 6.4.x, 6.5.x, 6.6.x | 1–2 | No passwords, OTP, recovery flows in the app | — | N/A |

### V8 Authorization

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 8.1.1 | 1 | Scope matrix (§5) documents function- and data-level rules | doc | Doc |
| 8.1.2 | 2 | Field-level rules: response models expose only listed fields; message bodies only with `actions:read` | doc + tests | Planned |
| 8.2.1 | 1 | Router-level deny-by-default dependency + per-route scope | parametrized test over all routes | Planned |
| 8.2.2 | 1 | Single-tenant data; object access checks by scope; ids are UUIDv7 (not an access control) | tests | Planned |
| 8.2.3 | 2 | `response_model` with explicit fields (no ORM objects returned) | tests | Planned |
| 8.3.1 | 1 | Authorization only server-side | — | Planned |
| 8.4.1 | 2 | Single tenant | — | N/A |

### V9 Self-contained Tokens

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 9.1.1 | 1 | Signature verified before reading claims | unit test (tampered token) | Planned |
| 9.1.2 | 1 | Algorithm allow-list `["EdDSA"]`; `none`/HS* rejected | unit tests | Planned |
| 9.1.3 | 1 | Keys only from configured public key/JWKS of the configured issuer; `jku`/`x5u`/`kid` URLs never fetched | unit test | Planned |
| 9.2.1 | 1 | `exp`/`nbf` required, 30 s leeway | unit tests | Planned |
| 9.2.2 | 2 | `typ=at+jwt` required (no ID tokens accepted) | unit test | Planned |
| 9.2.3 | 2 | `aud` must equal `FOLLOWUP_JWT_AUDIENCE` | unit test | Planned |
| 9.2.4 | 2 | Local issuer always sets `aud` | unit test | Planned |

### V10 OAuth and OIDC (resource-server role, production)

| ID | L | Control | Status |
|----|---|---------|--------|
| 10.3.1 | 2 | Audience check (same as 9.2.3) | Planned |
| 10.3.2 | 2 | Authorization from token `scope` claim only | Planned |
| 10.3.3 | 2 | Identity = `iss` + `sub` | Planned |
| 10.3.4 | 2 | No authentication-strength requirements | N/A |
| 10.1–10.2, 10.4–10.7 | 1–2 | Not an OAuth client, AS or OP | N/A |

### V11 Cryptography

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 11.1.1 | 2 | Key lifecycle: Ed25519 token keys generated per environment, rotated every 90 days, JWKS supports two active keys | doc | Doc |
| 11.1.2 | 2 | Crypto inventory (§7) | doc | Doc |
| 11.2.1 | 2 | Only `hashlib`, `secrets`, `cryptography` (via PyJWT) | dependency review | Planned |
| 11.2.2 | 2 | Algorithms are configuration (agility) | config test | Planned |
| 11.2.3 | 2 | ≥ 128-bit security (SHA-256, Ed25519) | doc | Planned |
| 11.3.x | 1–2 | No application-level encryption (DB/disk encryption by platform) | — | N/A / Prod |
| 11.4.1 | 1 | SHA-256 only; ruff `S324` bans MD5/SHA-1 | CI lint | Planned |
| 11.4.2–11.4.4 | 2 | No password storage or password-derived keys | — | N/A |
| 11.5.1 | 2 | `secrets` (CSPRNG) for `jti`, generated passwords. UUIDs (`operation_id`, ids) are identifiers, never secrets | unit test | Planned |
| 11.6.1 | 2 | Key generation via `cryptography` Ed25519 | — | Planned |

### V12 Secure Communication

| ID | L | Control | Status |
|----|---|---------|--------|
| 12.1.1 | 1 | Edge TLS ≥ 1.2 (1.3 preferred); outbound `httpx` uses default SSL context (TLS ≥ 1.2) | Prod / Planned |
| 12.1.2 | 2 | Cipher suites per OWASP TLS cheat sheet at the terminator | Prod |
| 12.1.3 | 2 | No mTLS client-certificate authentication | N/A |
| 12.2.1, 12.2.2 | 1 | Public endpoints only via TLS with publicly trusted certs | Prod |
| 12.3.1 | 2 | Internal TLS: DB `sslmode=verify-full`, OTLP over TLS, Prometheus scrape over TLS | Prod (**Deviation locally**: isolated compose networks, plain TCP) |
| 12.3.2 | 2 | Certificate verification always on; ruff `S501`; test asserts no `verify=False` | Planned |
| 12.3.3, 12.3.4 | 2 | Internal services use TLS with internal CA trusted by clients | Prod |

### V13 Configuration

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 13.1.1 | 2 | Communication inventory (§6) | doc | Doc |
| 13.2.1 | 2 | Service JWTs (D1); individual DB roles (D3); prod: dynamic DB creds, workload identity for LLM | tests + prod config | Planned / Prod (**Deviation locally**: static DB passwords and Gemini API key, generated per machine, never committed) |
| 13.2.2 | 2 | Least-privilege DB roles; read-only task API token; delivery token can only create messages | integration test (`followup_app` cannot `DROP`/`UPDATE audit_events`) | Planned |
| 13.2.3 | 2 | No default credentials anywhere (generated by `make secrets`) | check script | Planned |
| 13.2.4, 13.2.5 | 2 | Outbound allow-list in HTTP client factory (D4) + `internal: true` backend network | adapter test | Planned |
| 13.3.1 | 2 | Docker secrets locally (`/run/secrets`, mode 0600, git-ignored); secret manager in prod | config | Planned / Prod |
| 13.3.2 | 2 | Each container receives only its own secrets (compose `secrets:` per service) | compose review | Planned |
| 13.4.1 | 1 | `.dockerignore` allow-list → no `.git` in image | CI image inspection | Planned |
| 13.4.2 | 2 | `debug=False`, no `--reload`, `ENV=prod` guard | config test | Planned |
| 13.4.3 | 2 | No static file serving | — | Planned |
| 13.4.4 | 2 | `TRACE` → 405 | API test | Planned |
| 13.4.5 | 2 | OpenAPI/Swagger off in prod; metrics on internal port only | config test | Planned |

### V14 Data Protection

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 14.1.1 | 2 | Data classification (§8) | doc | Doc |
| 14.1.2 | 2 | Protection requirements per class (§8) | doc | Doc |
| 14.2.1 | 1 | Tokens only in `Authorization` header; no secrets/PII in URLs or query strings | API test | Planned |
| 14.2.2 | 2 | `Cache-Control: no-store`; LLM cache stores only validated interpretations (enum + ≤ 200-char evidence), 30-day TTL | tests | Planned |
| 14.2.3 | 2 | LLM data minimisation + provider tier without training on inputs (D11); no trackers | doc + payload test | Planned / Prod |
| 14.2.4 | 2 | Logging/retention/access per class implemented (`14` §6, §7) | tests | Planned |
| 14.3.1, 14.3.3 | 1–2 | No browser storage | — | N/A |
| 14.3.2 | 2 | `Cache-Control: no-store` on all API responses | API test | Planned |

### V15 Secure Coding and Architecture

| ID | L | Control | Verification | Status |
|----|---|---------|--------------|--------|
| 15.1.1 | 1 | Remediation SLAs: critical 7 d, high 30 d, medium 90 d; dependency update cadence weekly (Dependabot) | `SECURITY.md` | Doc |
| 15.1.2 | 2 | CycloneDX SBOM per build (CI artefact + image attestation); packages only from PyPI via `uv.lock` hashes | CI | Planned |
| 15.1.3 | 2 | Resource-heavy functions documented: full run, LLM calls, dispatch batches | `05`, `09` | Doc |
| 15.2.1 | 1 | `pip-audit` + Trivy gate in CI | CI | Planned |
| 15.2.2 | 2 | Limits for heavy functions: run concurrency 1, max tasks/run, LLM semaphore + budget, request body cap, timeouts | tests | Planned |
| 15.2.3 | 2 | Prod image = product package only (mocks/dev tools excluded) | CI image inspection test | Planned |
| 15.3.1 | 1 | Explicit response models | API tests | Planned |
| 15.3.2 | 2 | `follow_redirects=False` on all clients | adapter test | Planned |
| 15.3.3 | 2 | Request models `extra="forbid"`, separate input/output models | API test (extra field → 422) | Planned |
| 15.3.4 | 2 | Client IP only from trusted proxy (`--forwarded-allow-ips`) | config test | Planned |
| 15.3.5 | 2 | Strict Pydantic types, `mypy --strict`, no truthiness checks on security values | CI | Planned |
| 15.3.6 | 2 | No JavaScript | — | N/A |
| 15.3.7 | 2 | Duplicate query parameters rejected (400) | API test | Planned |

### V16 Security Logging and Error Handling

| ID | L | Control | Status |
|----|---|---------|--------|
| 16.1.1 | 2 | Log inventory (`14` §1) | Doc |
| 16.2.1 | 2 | Log schema with when/where/who/what (`14` §3) | Planned |
| 16.2.2 | 2 | UTC RFC 3339 timestamps | Planned |
| 16.2.3 | 2 | Destinations only as inventoried | Planned |
| 16.2.4 | 2 | JSON, common field names | Planned |
| 16.2.5 | 2 | Classification-based redaction (`14` §6) | Planned |
| 16.3.1 | 2 | Every token validation success/failure logged | Planned |
| 16.3.2 | 2 | Authorization denials logged | Planned |
| 16.3.3 | 2 | Validation, rate-limit, injection-screen and LLM-output rejections logged | Planned |
| 16.3.4 | 2 | Unexpected errors and TLS failures logged | Planned |
| 16.4.1 | 2 | JSON encoding prevents log injection (tested) | Planned |
| 16.4.2 | 2 | App cannot alter shipped logs; audit table append-only | Planned |
| 16.4.3 | 2 | Logs shipped to a separate system (Loki) | Planned (TLS: Prod) |
| 16.5.1 | 2 | Generic `problem+json` errors | Planned |
| 16.5.2 | 2 | Circuit breakers, template/keyword fallback | Planned |
| 16.5.3 | 2 | Fail closed: any validation/DB/LLM error → no send; unknown outcome → reconcile | Planned |

### Not applicable chapters

V5 (no file handling), V7 (no sessions), V17 (no WebRTC).

## 5. Authorization matrix (8.1.1)

| Route | Method | Scope | Data returned |
|-------|--------|-------|---------------|
| `/healthz`, `/readyz` | GET | public | `{"status": "ok"}` only |
| `/v1/runs` | POST | `runs:trigger` | run report (counts, no message bodies) |
| `/v1/runs/{run_id}` | GET | `runs:read` | run report |
| `/v1/actions` | GET | `actions:read` | actions incl. message bodies and targets |
| `/metrics` | GET | internal port, network-restricted | metrics |

Service accounts: `scheduler` (`runs:trigger`), `ops-dashboard`
(`runs:read`, `actions:read`), `reviewer-cli` (all three; local only).

## 6. Communication inventory (13.1.1)

| From → To | Protocol | Auth | Purpose | Timeouts / limits |
|-----------|----------|------|---------|-------------------|
| Client → API | HTTPS (prod) | service JWT | trigger/read | body ≤ 64 KiB, rate limit |
| App → Task API | HTTPS | read-only token (prod: OAuth) | fetch tasks | 5 s, 4 attempts, 20 s deadline |
| App → Delivery API | HTTPS | create-only token | send messages | 5 s, single attempt per dispatch |
| App → LLM provider | HTTPS | API key (local) / workload identity (prod) | interpret, draft | 10 s, 1 retry, semaphore 4, daily budget |
| App → PostgreSQL | TCP+TLS (prod) | `followup_app` role | state | pool 10, statement timeout 5 s |
| Migrator → PostgreSQL | TCP+TLS (prod) | `followup_migrator` | DDL | one-shot |
| App → Alloy | OTLP/HTTP(S) | network-restricted (prod: mTLS) | logs | batch, drop on overflow (counted) |
| Prometheus → App | HTTP(S) internal | network-restricted | metrics | 15 s |

No component accepts a URL from users or task data (no SSRF surface).

## 7. Crypto inventory (11.1.2)

| Use | Algorithm | Library | Key/secret | Rotation |
|-----|-----------|---------|-----------|----------|
| Service tokens | EdDSA (Ed25519) | PyJWT + cryptography | private key at issuer only; app has public key/JWKS | 90 days, overlap via `kid` |
| Fingerprint, idempotency key, cache key | SHA-256 | hashlib | none (integrity/identity, not secrecy) | — |
| Operation id | UUIDv5 (SHA-1 inside UUID spec — identifier only, not a security control) | uuid | none | — |
| Random secrets, `jti` | CSPRNG | `secrets` | — | — |
| Transport | TLS 1.2/1.3 | platform | certificates | platform |

## 8. Data classification (14.1.1 / 14.1.2)

| Class | Data | Storage | In logs | To LLM | Retention |
|-------|------|---------|---------|--------|-----------|
| Secret | tokens, keys, passwords, DSNs | secret store only | never | never | rotation policy |
| Confidential-personal | owner names, recipient targets, message bodies | DB (`actions`, `audit_events`) | ids only | never (placeholders) | 1 year |
| Untrusted content | task notes, LLM raw output | not stored raw (hash + length); validated evidence ≤ 200 chars in cache | hash only | note + title only | cache 30 days |
| Internal | task ids, decisions, rule ids, metrics | DB, Loki, Prometheus | yes | no | 30–90 days |

## 9. Threat model (STRIDE, condensed)

| Threat | Example | Mitigations |
|--------|---------|-------------|
| Spoofing | Forged API token | EdDSA signature, alg allow-list, `aud`/`iss`/`typ`, short `exp` |
| Tampering | Prompt injection in notes alters decisions | No LLM capability over decisions/targets, closed schemas, grounding, screen + audit |
| Repudiation | "Who triggered this run?" | `actor` (token `sub`) on every run, audit trail, append-only |
| Information disclosure | Notes/PII leaked via logs or to LLM | Classification, redaction, minimisation, placeholders |
| Denial of service | Flood of run triggers, giant fixture, LLM cost blow-up | Rate limits, size caps, run concurrency 1, budget + breaker |
| Elevation of privilege | App role altering audit or schema | Separate DB roles, REVOKE, read-only FS, non-root, `cap_drop: ALL` |
| Abuse case | State flapping causes message storm to one recipient | Per-recipient daily cap (D6), dedupe, alert |

## 10. OWASP Cheat Sheets to apply (by component)

| Component / activity | Cheat sheets |
|----------------------|--------------|
| FastAPI service | **FastAPI Security**, REST Security, REST Assessment, HTTP Headers, Error Handling, Input Validation, Mass Assignment, Denial of Service |
| AuthN/AuthZ | JSON Web Token, Authorization, Access Control, Insecure Direct Object Reference Prevention, Workload Identity Federation, Key Management |
| LLM integration | **LLM Prompt Injection Prevention**, Secure AI Model Ops, AI Agent Security (why we chose a workflow), Business Logic Security |
| Outbound integrations | Server Side Request Forgery Prevention, Webhook Security (future monday.com webhooks: signatures, replay, idempotency), Transport Layer Security, TLS Cipher String |
| Persistence | Query Parameterization, SQL Injection Prevention, Database Security, Cryptographic Storage |
| Secrets & config | Secrets Management, Key Management |
| Logging | **Logging**, **Logging Vocabulary**, Error Handling |
| Containers & CI | **Docker Security**, CI/CD Security, GitHub Actions Security, Infrastructure as Code Security, Network Segmentation |
| Supply chain | Vulnerable Dependency Management, Dependency Graph SBOM, Software Supply Chain Security |
| Process | Threat Modeling, Abuse Case, Secure Code Review, **Secure Coding with AI** (AI assistants are allowed by the brief: no hallucinated packages, review every dependency, no secrets in prompts), Vulnerability Disclosure, User Privacy Protection |

Each cheat sheet in **bold** gets a short checklist file under
`docs/security/` during M9, with every item mapped to a control or an N/A
reason, exactly like the ASVS tables above.

## 10a. OWASP API Security Top 10 (2023) mapping

| Risk | Our control |
|------|-------------|
| API1 Broken Object Level Authorization | Single tenant; scope per route; no user-owned objects; ids are UUIDv7 but never used as access control |
| API2 Broken Authentication | EdDSA service JWT, alg allow-list, `aud`/`iss`/`typ`/`exp` checks, auth-failure alerting |
| API3 Broken Object Property Level Authorization | Explicit response models, `extra="forbid"` request models |
| API4 Unrestricted Resource Consumption | rate limit, body cap, run lock, LLM budget, retry budget, per-recipient cap |
| API5 Broken Function Level Authorization | deny-by-default router dependency + route inventory test |
| API6 Unrestricted Access to Sensitive Business Flows | run trigger requires `runs:trigger`, single-run lock, audit of every trigger |
| API7 SSRF | no user-supplied URLs; outbound allow-list; no redirects |
| API8 Security Misconfiguration | docs off in prod, headers, TRACE 405, hardened containers, Trivy config |
| API9 Improper Inventory Management | versioned `/v1`, OpenAPI as contract, communication inventory (§6) |
| API10 Unsafe Consumption of APIs | task/delivery/LLM responses validated with strict schemas; timeouts; fail closed |

## 10b. Supply chain (SLSA-style practices) and CWE

* GitHub Actions pinned by commit SHA; container images (including
  scanners) pinned by digest; pre-commit hooks frozen to SHAs.
* Rationale is concrete: in March 2026 the Trivy release v0.69.4 and images
  0.69.5–0.69.6 were compromised and used to steal CI credentials, which led
  to malicious LiteLLM releases on PyPI from a pipeline that ran an
  unpinned scanner.
* `uv.lock` with hashes, `uv sync --locked`, `pip-audit --require-hashes`;
  Dependabot with a 7-day cooldown (new releases are not adopted on day 0).
* Image build with provenance + SBOM attestation; optional keyless signing
  (cosign) when a registry is used.
* Findings from Bandit and Semgrep are triaged by their **CWE id** with the
  CWE Top 25 as priority order.

## 11. Security verification in CI

| Check | Tool | Gate |
|-------|------|------|
| SAST | **Bandit** (standalone gate) + Ruff `S` (editor feedback) + Semgrep (`p/python`, `p/owasp-top-ten`, custom invariants) | Bandit: zero findings (≥ low sev., ≥ medium conf.); Semgrep: zero errors |
| Secrets | gitleaks (pre-commit + CI) | zero findings |
| Dependencies | `pip-audit` on exported lock | no known vulns beyond SLA |
| SBOM | CycloneDX (lock export) + image attestation | artefact present |
| Container | Trivy image + Dockerfile misconfig scan, Hadolint | no HIGH/CRITICAL with fix |
| Workflows | zizmor (GitHub Actions static analysis), actions pinned by SHA | zero high |
| DAST (smoke) | ZAP API scan against the running compose stack using the OpenAPI spec (CI only) | no high alerts |
| ASVS traceability | pytest `asvs` markers → `docs/asvs-traceability.md` | every *Planned* item has ≥ 1 test |
