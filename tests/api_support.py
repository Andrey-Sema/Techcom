"""Hermetic API fixtures with ephemeral keys and injected clocks."""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from support import AS_OF, make_task, repo_policy
from techcom_followup.adapters.inbound.api.app import create_app
from techcom_followup.adapters.inbound.api.middleware import ApiSettings
from techcom_followup.adapters.inbound.api.observability import Observer
from techcom_followup.adapters.inbound.api.ports import SecurityEvent
from techcom_followup.adapters.inbound.api.security import (
    TokenSettings,
    TokenVerifier,
)
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.application.repository import DecisionWrite, RunRecord
from techcom_followup.domain.identity import ActionIdentity, state_fingerprint
from techcom_followup.domain.messages import compose
from techcom_followup.domain.models import TaskId, TaskStatus

NOW = datetime(2026, 10, 21, 9, tzinfo=UTC)


@dataclass(slots=True)
class ApiClock:
    value: datetime = NOW
    elapsed: float = 0

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.value += timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class FakeRunResult:
    run_id: UUID | None = None
    status: str = "completed"
    enqueued: int = 0
    duplicates: int = 0
    stale: int = 0


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[date, bool]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail = False
        self.result = FakeRunResult()

    async def execute(
        self, *, as_of: date, dry_run: bool = False
    ) -> FakeRunResult:
        self.calls.append((as_of, dry_run))
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.fail:
            message = (
                "rawnote/secret: sensitive provider response must stay private"
            )
            raise RuntimeError(message)
        return self.result


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[SecurityEvent] = []

    def emit(self, event: SecurityEvent) -> None:
        self.events.append(event)


class Harness:
    def __init__(self, settings: ApiSettings | None = None) -> None:
        self.repo = InMemoryRepository()
        self.runner = FakeRunner()
        self.clock = ApiClock()
        self.key = Ed25519PrivateKey.generate()
        self.sink = RecordingSink()
        self.observer = Observer(self.sink)
        self.verifier = TokenVerifier(
            TokenSettings(
                "https://issuer.example.test",
                "followup-api",
                {
                    "active": self.key.public_key().public_bytes(
                        serialization.Encoding.PEM,
                        serialization.PublicFormat.SubjectPublicKeyInfo,
                    )
                },
                frozenset({"global"}),
            )
        )
        self.app = create_app(
            repository=self.repo,
            runner=self.runner,
            verifier=self.verifier,
            clock=self.clock,
            settings=settings,
            observer=self.observer,
        )

    def claims(self, **changes: JsonValue) -> dict[str, JsonValue]:
        now = int(self.clock.now().timestamp())
        base: dict[str, JsonValue] = {
            "iss": "https://issuer.example.test",
            "aud": "followup-api",
            "sub": "global",
            "jti": str(uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "scope": "runs:trigger runs:read actions:read",
            "projects": ["*"],
        }
        return base | changes

    def token(self, **changes: JsonValue) -> str:
        return self.sign(self.claims(**changes))

    def sign(self, claims: dict[str, JsonValue], **headers: JsonValue) -> str:
        return jwt.encode(
            claims,
            self.key,
            algorithm="EdDSA",
            headers={"typ": "at+jwt", "kid": "active"} | headers,
        )

    def client(self, token: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=self.app, raise_app_exceptions=False
            ),
            base_url="https://service.example.test",
            headers={}
            if token is None
            else {"Authorization": "Bearer " + token},
        )


async def seed_run(
    harness: Harness,
    projects: tuple[str | None, ...] = ("A",),
    *,
    quiet: bool = False,
    finish: bool = True,
    rejected: int = 0,
) -> RunRecord:
    run = await harness.repo.start_run(AS_OF, "2026-10-21.1", NOW)
    for index, project in enumerate(projects):
        task = make_task(
            task_id=TaskId(f"TC-{index}"),
            project=project or "A",
            status=TaskStatus.DONE if quiet else TaskStatus.BLOCKED,
        )
        decision = repo_policy().decide(task, as_of=AS_OF)
        if decision.target is not None:
            decision = replace(
                decision, target=replace(decision.target, project=project)
            )
        fingerprint = state_fingerprint(task)
        identity = (
            ActionIdentity.of(decision, fingerprint)
            if decision.actionable
            else None
        )
        message = (
            compose(decision, task, AS_OF) if decision.actionable else None
        )
        await harness.repo.record_decision(
            DecisionWrite(
                run.id,
                decision,
                fingerprint,
                identity,
                message,
                source_project=None if quiet else project,
            ),
            NOW,
        )
    if finish:
        await harness.repo.finish_run(
            run.id,
            {
                "status": "completed",
                "decisions": len(projects),
                "rejected": rejected,
                "enqueued": len(projects),
                "duplicates": 0,
            },
            NOW,
        )
    current = await harness.repo.get_run(run.id)
    assert current is not None
    return current
