"""Authenticated API with server-derived project access."""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import compile_path

from techcom_followup.adapters.inbound.api.middleware import (
    ApiSettings,
    SecurityMiddleware,
    problem,
)
from techcom_followup.adapters.inbound.api.models import (
    ActionQuery,
    ActionView,
    RunCounts,
    RunView,
    TargetView,
    TriggerRequest,
    TriggerView,
)
from techcom_followup.adapters.inbound.api.observability import Observer
from techcom_followup.adapters.inbound.api.ports import (
    Clock,
    RequestLimiter,
    RunService,
    SecurityEvent,
)
from techcom_followup.adapters.inbound.api.rate_limit import (
    LocalRequestLimiter,
)
from techcom_followup.adapters.inbound.api.security import (
    AuthenticationError,
    Principal,
    TokenVerifier,
)
from techcom_followup.application.repository import (
    ActionReadQuery,
    ActionRecord,
    Repository,
    RunRecord,
    StoredDecision,
)
from techcom_followup.domain.models import Target

ROUTE_SCOPES = {
    ("POST", "/v1/runs"): "runs:trigger",
    ("GET", "/v1/runs/{run_id}"): "runs:read",
    ("GET", "/v1/actions"): "actions:read",
}


@dataclass(frozen=True, slots=True)
class _Resources:
    repository: Repository
    runner: RunService
    verifier: TokenVerifier
    clock: Clock
    settings: ApiSettings
    observer: Observer
    limiter: RequestLimiter


def _correlation(request: Request) -> UUID:
    return UUID(str(request.state.request_id))


def _run_view(run: RunRecord) -> RunView:
    stats = run.stats or {}
    counts = RunCounts.model_validate(
        {key: stats[key] for key in RunCounts.model_fields if key in stats}
    )
    status: Literal["running", "completed", "failed"] = (
        "running"
        if run.finished_at is None
        else ("completed" if stats.get("status") == "completed" else "failed")
    )
    return RunView(
        run_id=run.id,
        as_of=run.as_of,
        policy_version=run.policy_version,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=status,
        counts=counts,
    )


def _action_view(action: ActionRecord, target: Target) -> ActionView:
    return ActionView(
        action_id=action.id,
        operation_id=action.identity.operation_id,
        task_id=action.decision.task_id,
        decision=action.decision.kind,
        status=action.status,
        target=TargetView(
            kind=target.kind, name=target.name, project=target.project
        ),
        subject=action.message.subject,
        body=action.message.body,
        attempts=action.attempts,
        next_attempt_at=action.next_attempt_at,
    )


class _Handlers:
    """Authorize each request before calling its use case."""

    def __init__(self, resources: _Resources) -> None:
        self.resources = resources
        self._trigger_lock = asyncio.Lock()

    def event(
        self,
        request: Request,
        name: str,
        status: int,
        actor: str | None = None,
    ) -> None:
        """Send a controlled event name and hashed actor identifier."""
        self.resources.observer.emit(
            SecurityEvent(name, _correlation(request), status, actor)
        )

    async def authenticate(self, request: Request) -> Principal:
        """Verify signatures and apply limits across rotated tokens."""
        cached = getattr(request.state, "api_principal", None)
        if isinstance(cached, Principal):
            return cached
        headers = request.headers.getlist("authorization")
        if len(headers) != 1 or not headers[0].startswith("Bearer "):
            await self._unauthenticated(request)
        try:
            principal = self.resources.verifier.verify(
                headers[0][len("Bearer ") :], self.resources.clock.now()
            )
        except AuthenticationError:
            await self._unauthenticated(request)
        if not await self.resources.limiter.allow(
            "requests", principal.actor_id
        ):
            self.event(
                request, "security.rate_limited", 429, principal.actor_id
            )
            raise HTTPException(
                429,
                headers={"Retry-After": self.resources.limiter.retry_after},
            )
        self.event(
            request, "security.authn_succeeded", 200, principal.actor_id
        )
        return principal

    async def preflight(self, request: Request) -> None:
        """Authenticate business routes before reading their body."""
        path = _relative_path(request)
        if path != "/v1" and not path.startswith("/v1/"):
            if path not in {"/healthz", "/readyz"}:
                raise HTTPException(404)
            return
        if request.method not in {method for method, _ in ROUTE_SCOPES}:
            raise HTTPException(405)
        principal = await self.authenticate(request)
        self.scope(request, principal, _required_scope(request.method, path))
        request.state.api_principal = principal

    async def _unauthenticated(self, request: Request) -> NoReturn:
        peer = request.client.host if request.client is not None else "unknown"
        key = hashlib.sha256(peer.encode()).hexdigest()
        if not await self.resources.limiter.allow("failures", key):
            self.event(request, "security.rate_limited", 429)
            raise HTTPException(
                429,
                headers={"Retry-After": self.resources.limiter.retry_after},
            )
        self.event(request, "security.authn_failed", 401)
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})

    def scope(
        self, request: Request, principal: Principal, required: str
    ) -> None:
        """Check capability and project authority before global work."""
        if required not in principal.scopes or (
            required == "runs:trigger" and not principal.all_projects
        ):
            self.event(
                request, "security.authz_denied", 403, principal.actor_id
            )
            raise HTTPException(403)

    async def trigger(
        self, request: Request, payload: TriggerRequest, principal: Principal
    ) -> TriggerView:
        """Allow global processing only to all-project services."""
        self.scope(request, principal, "runs:trigger")
        if self._trigger_lock.locked():
            raise HTTPException(409)
        async with self._trigger_lock:
            result = await self.resources.runner.execute(
                as_of=payload.as_of, dry_run=payload.dry_run
            )
        return TriggerView.model_validate(
            {
                "run_id": result.run_id,
                "status": result.status,
                "enqueued": result.enqueued,
                "duplicates": result.duplicates,
            }
        )

    async def read_run(
        self, request: Request, run_id: UUID, principal: Principal
    ) -> RunView:
        """Hide missing and unauthorized runs identically."""
        self.scope(request, principal, "runs:read")
        run = await self.resources.repository.get_run(run_id)
        if run is None or not await self._run_accessible(run, principal):
            self.event(
                request,
                "security.object_access_denied",
                404,
                principal.actor_id,
            )
            raise HTTPException(404)
        return _run_view(run)

    async def _run_accessible(
        self, run: RunRecord, principal: Principal
    ) -> bool:
        if principal.all_projects:
            return True
        if not _complete(run):
            return False
        decisions = await self.resources.repository.list_decisions(run.id)
        if not decisions or (run.stats or {}).get("decisions") != len(
            decisions
        ):
            return False
        return all(_decision_accessible(row, principal) for row in decisions)

    async def read_actions(
        self, request: Request, query: ActionQuery, principal: Principal
    ) -> list[ActionView]:
        """Apply project authorization before filters and limits."""
        self.scope(request, principal, "actions:read")
        actions = await self.resources.repository.query_actions(
            ActionReadQuery(
                projects=(
                    frozenset()
                    if principal.all_projects
                    else principal.projects
                ),
                all_projects=principal.all_projects,
                project=query.project,
                task_id=query.task_id,
                status=query.status,
                limit=query.limit,
            )
        )
        return [
            _action_view(action, target)
            for action in actions
            if (target := action.decision.target) is not None
            and principal.can_access(target.project)
            and principal.can_access(action.source_project)
        ]


def _complete(run: RunRecord) -> bool:
    stats = run.stats or {}
    return (
        run.finished_at is not None
        and stats.get("status") == "completed"
        and stats.get("rejected") == 0
    )


def _decision_accessible(row: StoredDecision, principal: Principal) -> bool:
    source, target = row.write.source_project, row.write.decision.target
    return (
        source is not None
        and principal.can_access(source)
        and (target is None or principal.can_access(target.project))
    )


def _relative_path(request: Request) -> str:
    path = str(request.scope["path"])
    root = str(request.scope.get("root_path", ""))
    if root and path.startswith(root):
        suffix = path[len(root) :]
        if not suffix or suffix.startswith("/"):
            return suffix
    return path


def _required_scope(method: str, path: str) -> str:
    for (candidate_method, template), required in ROUTE_SCOPES.items():
        pattern, _format, _converters = compile_path(template)
        if method == candidate_method and pattern.fullmatch(path):
            return required
    return ""


def _routes(handlers: _Handlers) -> APIRouter:
    async def guard(
        request: Request,
        principal: Annotated[Principal, Depends(handlers.authenticate)],
    ) -> None:
        required = ROUTE_SCOPES.get(
            (request.method, request.scope["route"].path), ""
        )
        handlers.scope(request, principal, required)

    router = APIRouter(prefix="/v1", dependencies=[Depends(guard)])

    @router.post("/runs")
    async def trigger(
        request: Request,
        payload: TriggerRequest,
        principal: Annotated[Principal, Depends(handlers.authenticate)],
    ) -> TriggerView:
        return await handlers.trigger(request, payload, principal)

    @router.get("/runs/{run_id}")
    async def read_run(
        request: Request,
        run_id: UUID,
        principal: Annotated[Principal, Depends(handlers.authenticate)],
    ) -> RunView:
        return await handlers.read_run(request, run_id, principal)

    @router.get("/actions")
    async def read_actions(
        request: Request,
        query: Annotated[ActionQuery, Query()],
        principal: Annotated[Principal, Depends(handlers.authenticate)],
    ) -> list[ActionView]:
        return await handlers.read_actions(request, query, principal)

    return router


def _errors(app: FastAPI, handlers: _Handlers) -> None:
    async def http_error(request: Request, error: Exception) -> JSONResponse:
        status = (
            error.status_code
            if isinstance(error, StarletteHTTPException)
            else 422
        )
        headers = (
            error.headers
            if isinstance(error, StarletteHTTPException)
            else None
        )
        handlers.event(request, "security.request_rejected", status)
        return problem(status, _correlation(request), headers=headers)

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, http_error)


# Keyword-only collaborators; the limiter adds shared windows.
def create_app(  # noqa: PLR0913
    *,
    repository: Repository,
    runner: RunService,
    verifier: TokenVerifier,
    clock: Clock,
    settings: ApiSettings | None = None,
    observer: Observer | None = None,
    limiter: RequestLimiter | None = None,
) -> FastAPI:
    """Build an API from explicit application dependencies."""
    configured = settings if settings is not None else ApiSettings()
    resources = _Resources(
        repository,
        runner,
        verifier,
        clock,
        configured,
        observer if observer is not None else Observer(),
        limiter
        if limiter is not None
        else LocalRequestLimiter(configured, clock.monotonic),
    )
    handlers = _Handlers(resources)
    app = FastAPI(
        title="TechCom follow-up service",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SecurityMiddleware,
        settings=configured,
        observer=resources.observer,
        guard=handlers.preflight,
    )
    app.include_router(_routes(handlers))
    _errors(app, handlers)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        await repository.get_run(UUID(int=0))
        return {"status": "ok"}

    return app


def create_metrics_app(observer: Observer) -> FastAPI:
    """Build metrics for a separate internal listener."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, debug=False)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(
            generate_latest(observer.registry), media_type=CONTENT_TYPE_LATEST
        )

    return app
