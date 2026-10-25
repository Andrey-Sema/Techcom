"""Catalogued metrics are emitted by the code paths that own them."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from persistence_support import NOW
from processing_support import Gateway, Source, fixture_records
from support import AS_OF, repo_policy
from techcom_followup.adapters.outbound.clock import FixedClock
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.llm.config import (
    RouteConfig,
    RoutingConfig,
)
from techcom_followup.adapters.outbound.llm.openai_responses import (
    BackendReply,
)
from techcom_followup.adapters.outbound.llm.router import LLMRouter
from techcom_followup.adapters.outbound.llm_agent_stub import (
    OfflineLanguageModel,
)
from techcom_followup.adapters.outbound.llm_stub import ScriptedLLM
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.dispatch import (
    DeliveryDependencies,
    DispatchActions,
)
from techcom_followup.application.llm_workflow import (
    InvalidOutputError,
    LanguageCall,
)
from techcom_followup.application.orchestrator import FollowUpOrchestrator
from techcom_followup.application.ports import (
    LLMError,
    LLMPurpose,
    LLMRequest,
    TokenUsage,
)
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.application.processing_ports import (
    DeliveryKind,
    DeliveryResult,
)
from techcom_followup.application.reconcile import ReconcileUnknown
from techcom_followup.application.repository import (
    ActionStatus,
    OutboxStatus,
)
from techcom_followup.infrastructure.resilience import CircuitBreaker
from techcom_followup.infrastructure.telemetry import PrometheusTelemetry

POLICY = repo_policy()


def value(
    telemetry: PrometheusTelemetry, name: str, **labels: str
) -> float | None:
    return telemetry.registry.get_sample_value(name, labels)


def processor(
    repository: InMemoryRepository, telemetry: PrometheusTelemetry
) -> ProcessTasks:
    return ProcessTasks(
        ProcessingDependencies(
            repository,
            Source(fixture_records()),
            POLICY,
            FixedClock(NOW),
            InMemoryRunLock(),
            telemetry,
        ),
        planner=FollowUpOrchestrator(
            OfflineLanguageModel(), repository, POLICY, telemetry=telemetry
        ),
    )


async def test_processing_counts_runs_tasks_decisions_and_actions() -> None:
    telemetry, repository = PrometheusTelemetry(), InMemoryRepository()
    await processor(repository, telemetry).execute(as_of=AS_OF)
    await processor(repository, telemetry).execute(as_of=AS_OF)
    assert value(telemetry, "followup_runs_total", result="completed") == 2
    assert value(telemetry, "followup_run_duration_seconds_count") == 2
    assert value(telemetry, "followup_tasks_total", outcome="processed") == 20
    assert value(telemetry, "followup_tasks_total", outcome="rejected") == 0
    assert (
        value(
            telemetry,
            "followup_decisions_total",
            decision="request_eta",
            rule="R5",
        )
        == 4
    )
    assert value(telemetry, "followup_actions_total", outcome="enqueued") == 7
    assert (
        value(
            telemetry,
            "followup_actions_total",
            outcome="duplicate_suppressed",
        )
        == 7
    )
    assert (
        value(telemetry, "followup_security_events_total", kind="note_flagged")
        == 2
    )
    assert (
        value(telemetry, "followup_agent_plans_total", outcome="accepted") == 7
    )
    assert (
        value(
            telemetry,
            "followup_llm_requests_total",
            purpose="orchestrate",
            result="ok",
        )
        == 21
    )


async def test_locked_run_is_counted() -> None:
    telemetry, repository = PrometheusTelemetry(), InMemoryRepository()
    lock = InMemoryRunLock()
    async with lock.acquire():
        report = await ProcessTasks(
            ProcessingDependencies(
                repository,
                Source([]),
                POLICY,
                FixedClock(NOW),
                lock,
                telemetry,
            )
        ).execute(as_of=AS_OF)
    assert report.status == "skipped_locked"
    assert (
        value(telemetry, "followup_runs_total", result="skipped_locked") == 1
    )


async def test_delivery_and_reconciliation_outcomes_are_counted() -> None:
    telemetry, repository = PrometheusTelemetry(), InMemoryRepository()
    await processor(repository, telemetry).execute(as_of=AS_OF)
    gateway = Gateway(result=DeliveryResult(DeliveryKind.UNKNOWN))
    gateway.lookup_result = DeliveryResult(DeliveryKind.ACCEPTED, "ref")
    clock = FixedClock(NOW)
    dependencies = DeliveryDependencies(repository, gateway, clock, telemetry)
    await DispatchActions(dependencies).execute(limit=10)
    clock.instant += timedelta(minutes=6)
    await ReconcileUnknown(dependencies).execute(limit=10)
    assert (
        value(telemetry, "followup_delivery_attempts_total", result="unknown")
        == 7
    )
    assert value(telemetry, "followup_actions_total", outcome="unknown") == 7
    assert (
        value(telemetry, "followup_actions_total", outcome="reconciled") == 7
    )


async def test_permanent_failures_and_absent_lookups() -> None:
    telemetry, repository = PrometheusTelemetry(), InMemoryRepository()
    await processor(repository, telemetry).execute(as_of=AS_OF)
    clock = FixedClock(NOW)
    unknown = DeliveryDependencies(
        repository,
        Gateway(result=DeliveryResult(DeliveryKind.UNKNOWN)),
        clock,
        telemetry,
    )
    await DispatchActions(unknown).execute(limit=3)
    clock.instant += timedelta(minutes=6)
    # Absent on lookup: back to pending, which is not an outcome.
    await ReconcileUnknown(unknown).execute(limit=10)
    assert (
        value(telemetry, "followup_actions_total", outcome="reconciled")
        is None
    )
    permanent = DeliveryDependencies(
        repository,
        Gateway(result=DeliveryResult(DeliveryKind.PERMANENT)),
        clock,
        telemetry,
    )
    clock.instant += timedelta(days=1)
    await DispatchActions(permanent).execute(limit=10)
    permanent_attempts = value(
        telemetry, "followup_delivery_attempts_total", result="permanent"
    )
    assert permanent_attempts
    assert (
        value(telemetry, "followup_actions_total", outcome="dead")
        == permanent_attempts
    )


async def test_language_call_results_by_purpose() -> None:
    telemetry = PrometheusTelemetry()
    request = LLMRequest(
        purpose=LLMPurpose.INTERPRET_NOTE,
        instructions="",
        input_text="",
        schema_name="s",
        json_schema={},
        prompt_version="interp.v1",
    )
    call = LanguageCall(
        ScriptedLLM(["ok", "bad", LLMError("down")]), telemetry=telemetry
    )

    def validate(text: str) -> str:
        if text == "bad":
            stage = "schema"
            raise InvalidOutputError(stage)
        return text

    for _ in range(3):
        await call.call(request, validate)
    await LanguageCall(None, telemetry=telemetry).call(request, validate)
    for result in ("ok", "invalid", "error", "unavailable"):
        assert (
            value(
                telemetry,
                "followup_llm_requests_total",
                purpose="interpret_note",
                result=result,
            )
            == 1
        ), result


class OneReply:
    async def complete(
        self, request: LLMRequest, route: RouteConfig, timeout_s: float
    ) -> BackendReply:
        return BackendReply(
            text="{}",
            model=route.model,
            response_id="r",
            usage=TokenUsage(input_tokens=1_000, output_tokens=200),
        )


async def test_router_reports_cost_tokens_latency_and_circuits(
    routing_config: RoutingConfig, request_: LLMRequest
) -> None:
    telemetry = PrometheusTelemetry()
    router = LLMRouter(
        config=routing_config,
        backends={"openai": OneReply()},
        monotonic=lambda: 0.0,
        today=lambda: date(2026, 10, 21),
        telemetry=telemetry,
    )
    await router.generate_json(request_)
    purpose = request_.purpose.value
    assert (
        value(telemetry, "followup_llm_cost_usd_total", purpose=purpose)
        == 0.0002
    )
    assert (
        value(
            telemetry,
            "followup_llm_tokens_total",
            purpose=purpose,
            direction="input",
        )
        == 1000
    )
    assert (
        value(telemetry, "followup_llm_latency_seconds_count", purpose=purpose)
        == 1
    )


def test_circuit_gauge_follows_the_breaker() -> None:
    telemetry, now = PrometheusTelemetry(), [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_s=10, monotonic=lambda: now[0]
    )
    telemetry.watch_circuit("delivery", lambda: breaker.state.value)
    breaker.record_failure()
    assert (
        value(
            telemetry,
            "followup_circuit_state",
            dependency="delivery",
            state="open",
        )
        == 1
    )
    now[0] = 11
    assert (
        value(
            telemetry,
            "followup_circuit_state",
            dependency="delivery",
            state="half_open",
        )
        == 1
    )


def test_outbox_gauges_ignore_terminal_due_times(tmp_path: Path) -> None:
    telemetry = PrometheusTelemetry()
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    telemetry.set_outbox(
        (
            OutboxStatus(ActionStatus.PENDING, 3, now - timedelta(hours=1)),
            OutboxStatus(ActionStatus.SENT, 9, now - timedelta(days=9)),
            OutboxStatus(ActionStatus.UNKNOWN, 1, now + timedelta(hours=1)),
        ),
        now,
    )
    assert value(telemetry, "followup_outbox_backlog", status="pending") == 3
    assert value(telemetry, "followup_outbox_backlog", status="dead") == 0
    assert (
        value(
            telemetry, "followup_outbox_oldest_age_seconds", status="pending"
        )
        == 3600
    )
    assert (
        value(telemetry, "followup_outbox_oldest_age_seconds", status="sent")
        == 0
    )
    assert (
        value(
            telemetry, "followup_outbox_oldest_age_seconds", status="unknown"
        )
        == 0
    )
    path = tmp_path / "followup.prom"
    telemetry.write_textfile(path)
    assert 'followup_outbox_backlog{status="pending"} 3.0' in path.read_text()


async def test_outbox_summary_groups_by_status() -> None:
    repository = InMemoryRepository()
    await processor(repository, PrometheusTelemetry()).execute(as_of=AS_OF)
    (pending,) = await repository.outbox_summary()
    assert pending.status is ActionStatus.PENDING
    assert pending.count == 7
    assert pending.earliest_due == NOW
