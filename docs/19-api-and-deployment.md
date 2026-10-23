# 19 — API and Deployment

The authenticated API (`followup-api`) triggers runs and reads run
reports and actions. It is optional for the exercise: the CLI covers the
whole workflow. This page describes its authority model and how to run
it with PostgreSQL, the private metrics listener and OTLP log export.

## Authority and request bounds

The API factory takes explicit repository, processing, verification, clock
and observer dependencies. The actual `ProcessTasks` implementation meets
the run-service port; scenario tests exercise API authentication, processing,
outbox creation, project-filtered reads and duplicate suppression together.
No deployment credential or live provider is needed for these tests.

* JWT verification uses configured Ed25519 public keys only: exact algorithm
  and token type, signature, issuer, audience, required issued/not-before/
  expiry times, a maximum 15-minute lifetime and bounded clock leeway.
  Token-supplied remote key URLs and unknown header fields are rejected.
* Every business route requires a declared scope. Signed project memberships
  authorize object reads; query fields only narrow those memberships. A
  wildcard additionally requires an explicitly privileged service subject.
  Global processing rejects a project-only subject before reading its body.
* UUIDs are identifiers, not authorization. Every decision persists its
  source project; scoped run reports fail closed for incomplete,
  ambiguous or mixed-project decision graphs and for historical rows
  without provenance.
* Authentication, capability admission and subject rate limiting run before
  request JSON parsing. Bodies have an actual-byte limit and a total five-
  second receive deadline (configurable within a finite bound). Limits also
  cover query bytes, duplicate fields, response rows and token length.
* `ActionReadQuery` carries server-derived authority to both repositories.
  PostgreSQL filters before `LIMIT`; application row decoding is bounded to
  100. Its current JSON project predicate has no dedicated index, so this
  bounds materialization, not database scan cost.
* Generic errors cover guards, receive failures, validation and dependency
  exceptions without echoing their text. Cancellation and lifespan failures
  propagate. Request IDs and security headers survive rejection paths.
  Only the two declared health paths are public; accidentally added root
  routes are rejected before their handlers execute.
* Metrics use a separate ASGI application and listener with a private
  registry. The public API does not expose its metrics route.
* Request windows are shared by all API replicas through PostgreSQL
  (`admit_api_request`); a database error denies the request. Rate
  limiting does not replace project authorization.

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
pinned to its registry-reported digest. An override of `ZAP_IMAGE` must be a reviewed
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

Registry publication and attestation are deployment operations; the
workflow only prepares them.
