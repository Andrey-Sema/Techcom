# Final integration: performance, security and operations

This change builds on merged PR #14 and combines the M6b/M9/M12 remainder,
performance work and M7 documentation in one new PR. It is a single-agent
implementation with two manual review passes: correctness/lifecycle and
security/trust boundaries. It does
not claim a new independent reviewer or successful execution of this patch.

## Implemented paths

| Area | Concrete behavior | Evidence prepared |
|---|---|---|
| Throughput | Eight bounded writers; one fewer database round trip for actionable decisions; indexed message batches and run-scoped reports; stable source ordering | 10,000-task gates at 60/30 seconds; Hypothesis schedule/order/dedupe properties; writer failure/cancellation cases |
| Project authority | Normalized source project persists with quiet/actionable decisions; source and target must agree; historical unknowns fail closed | Project query contracts and quiet-run authorization regression |
| Shared API limits | Atomic PostgreSQL windows, separate auth-failure accounting, bounded subjects; database errors deny work | Independent limiter instances racing the same capacity and cardinality |
| Recipient limits | Durable logical-operation admission, owned lease, UTC day; default 20 per recipient/day; no uncertain-send refunds | Concurrent admissions, repeated operations, expired tokens, and no-send deferral tests |
| Retention | Immutable identity ledger, actual terminal timestamps, finished-run cutoffs | Payload-prune/replay, old active runs, recent completion and DEAD aging contracts |
| Observability | Real OTLP/HTTP JSON export, closed-field projection, bounded thread-safe queue, export/drop metrics, separate private metrics listener | HTTP serialization/privacy/partial-rejection and arbitrary-input properties |
| DAST | `make zap` scans an isolated synthetic API with ephemeral read-only credentials | Reproducible target/schema/credential preparation; no live provider or delivery network |
| Image provenance | Manual main-only GHCR publication workflow with digest-bound GitHub attestation and BuildKit provenance/SBOM | Pinned action revisions; verification command documented below |

## Runtime configuration

`followup-api` requires `FOLLOWUP_DATABASE_URL`, API issuer, audience and
`FOLLOWUP_API_PUBLIC_KEYS_FILE` (JSON map of key id to public Ed25519 PEM).
It never loads a private issuer key. Grant the provisioned login membership
in `followup_app`; migrate separately as the schema owner to `0004`.
Startup checks the required columns and shared-admission permission before
opening listeners. It rejects credentials that can erase identities,
mutate identity reservations, rewrite/erase audits, create schema objects,
or carry administrative role
flags on the effective/session user. Processing remains lazy: authenticated
read endpoints do not require LLM credentials or initialize a model client.

The API defaults to `127.0.0.1:8080`; metrics default to
`127.0.0.1:9464`. `compose.api.yaml` joins the existing observability network
and keeps 9464 unpublished; Prometheus already targets `app:9464`.
Use the three compose files together:

```sh
docker compose -f compose.yaml -f compose.observability.yaml -f compose.api.yaml up -d
```

Provision `secrets/api_database_url` and `secrets/api_public_keys.json`.
The database URL file must be readable by container UID 10001 and remain
private on the host; local Compose bind-mounted secrets do not remap file
ownership. Use an orchestrator secret mount or an explicit host ACL. Never
make database credentials world-readable to work around a mount problem.
Database migrations and collector certificates are operator prerequisites.
Configure `FOLLOWUP_API_ALL_PROJECT_SUBJECTS` as an explicit JSON subject
list for services that may trigger global processing; its default is empty.

OTLP destinations require an explicit approved host and HTTPS. Plain HTTP
requires `FOLLOWUP_API_ALLOW_INTERNAL_HTTP=true`; the compose example uses
it only on the private Alloy network. Redirects are disabled, export has
finite request/drain deadlines, and failed/overflowed records increment
metrics. Collector acknowledgements are limited to 4 KiB of wire data,
without unrequested decompression. Stdlib access logs are disabled. Logs preserve only the
closed projection in `log_config.py`; provider text and credentials never
enter the exporter. CLI commands keep their sanitized stderr logger; the
API process owns the OTLP lifecycle.

Tune `FOLLOWUP_WRITE_CONCURRENCY` only within 1..16 and provision the pool
for those writers plus the advisory-lock connection. The runtime does so
explicitly and sets SQL/lock timeouts. Set `FOLLOWUP_RECIPIENT_DAILY_LIMIT`
consistently across dispatchers. API window/capacity/subject settings must
also match across replicas. Pseudonymous quota history expires through the
maintenance function; `action_keys` survives permanently and must be in
full-database backups/restores. Restoring only action payloads or clearing
the ledger does not preserve duplicate suppression.
Backfill reads both existing actions and surviving decision receipts. Keys
already lost from both before migration cannot be reconstructed; recover
them from a reviewed backup before enabling historical replays. Schedule
the backfill in a maintenance window; it takes table locks and rejects a
lock wait beyond five seconds.
Pause dispatch and maintenance until every worker has the new quota and
retention code. A database trigger fills terminal timestamps for legacy
writers, but an old dispatcher cannot enforce the new recipient policy.

## ZAP and registry operations

Run `make images` before `make zap`. The default official GHCR image is
pinned to its registry-reported digest (metadata read on 2026-10-23;
the image was not executed). An override of `ZAP_IMAGE` must be a reviewed
`ghcr.io/zaproxy/zaproxy[:tag]@sha256:<digest>`; mutable tags are rejected.
The image is pulled before minting the short-lived scan token. Preparation
keeps the signing key in memory and creates a 15-minute, read-only token for
synthetic data. An authenticated probe must read a nonempty fixture response
before the scanner starts, preventing an unauthenticated scan from appearing
successful. The scan imports a private schema; the
public API continues to expose no OpenAPI route. The audit network is
internal and publishes no ports. Public program/key fixture mounts are
separate from the owner-only host folder containing the token and reports.
The scanner UID follows the invoking host UID, with dropped capabilities
and a read-only root filesystem. DAST deliberately raises local request
capacity to let the scan progress; shared rate semantics have separate
integration tests. POST processing remains unauthorized for its token.

The `Publish attested runtime` workflow is manual, main-only and uses the
`release` environment. Configure its approval rules and GHCR access before
dispatch. It does not run from pull requests and does not accept an
untrusted checkout ref. Verify the published immutable digest, repository
identity and workflow through GitHub CLI:

```sh
gh attestation verify oci://ghcr.io/andrey-sema/techcom@sha256:<digest> \
  --repo Andrey-Sema/Techcom \
  --signer-workflow Andrey-Sema/Techcom/.github/workflows/publish.yaml \
  --source-ref refs/heads/main
```

Registry publication, repository-plan attestation support and an actual
signature are external prerequisites/results. Adding the workflow is not
evidence that an image has already been published or attested.

## Manual review findings resolved

* Joined writers before run finalization; a silently cancelled child cannot
  mark an incomplete run completed. Reports avoid UUID scheduling order.
* Kept ledger reservations and outbox insertion in one transaction; failed
  audit/decision writes roll back the identity reservation as well.
* Kept security-definer SQL free of dynamic identifiers, qualified all data
  tables, fixed `search_path`, revoked public execution and denied direct
  mutation of ledger/limit tables to service and maintenance roles.
  `pg_temp` is explicitly last in the function search path to prevent
  temporary type/relation shadowing.
* Checked source project independently of recipient project. Historical
  unknown provenance is denied rather than inferred during migration.
  Display cleanup cannot establish project authority: noncanonical project
  names are rejected instead of truncated or stripped into another scope;
  missing-project placeholders carry no scoped authority.
* Serialized new API subjects with their capacity check, preventing a race
  around the cardinality limit; database time avoids replica clock skew.
  Admission functions require READ COMMITTED for post-lock visibility and
  reject null arguments without mutating counters. Runtime pools select
  that isolation level explicitly.
* Fenced recipient reservations by action/token/lease, reused reservations
  on retries, and preserved uncertain-send accounting. Breaker probe
  ownership is released on denied/error quota paths.
* Bounded OTLP queue, batches, outbound timeout and shutdown; used a
  thread-safe queue and sanitized records before export. Malformed or
  partially rejected collector responses are failed exports.
* Preserved unfinished runs and recently terminal work. Protected current
  quota history even from an overly broad maintenance cutoff.

## Unexecuted work and remaining limits

No tests, builds, linters, scanners, application, DAST, publishing workflow
or benchmark were run for this extension. Formatting only edits source
text. Earlier test counts in document 22 apply to the earlier API/evaluation
revision, not to this patch. The new timing and 100% coverage gates remain
unverified. CI minutes/billing and Dependabot PRs #4–#7 are unchanged.

The API needs a trusted TLS gateway with connection/header limits; fixed
windows may permit bursts across their boundary. Recipient names are the
current domain identity, so address/alias resolution needs an authoritative
directory before an email-provider integration. LLM provider quality is
still unverified, budgets remain process-local, and no broad injection
immunity or complete ASVS certification is asserted. A live-review run is
documented in M7 material but intentionally not rehearsed in this environment.
