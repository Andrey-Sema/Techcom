import asyncio
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from llm_doubles import FakeClock
from techcom_followup.adapters.outbound.llm.accounting import maximum_usage
from techcom_followup.adapters.outbound.llm.config import (
    RouteConfig,
    RoutingConfig,
)
from techcom_followup.adapters.outbound.llm.openai_responses import (
    BackendError,
    BackendReply,
    FailureKind,
)
from techcom_followup.adapters.outbound.llm.router import LLMRouter
from techcom_followup.application.ports import (
    LLMBudgetExceededError,
    LLMConfigurationError,
    LLMIncompleteError,
    LLMRefusalError,
    LLMRequest,
    LLMUnavailableError,
    TokenUsage,
)

USAGE = TokenUsage(input_tokens=1_000, output_tokens=200)


class ScriptedBackend:
    """Returns or raises a scripted outcome per model."""

    def __init__(
        self,
        outcomes: dict[str, BackendReply | Exception],
        clock: FakeClock | None = None,
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.clock = clock

    async def complete(
        self,
        request: LLMRequest,
        route: RouteConfig,
        timeout_s: float,
    ) -> BackendReply:
        self.calls.append(route.model)
        if self.clock is not None:
            self.clock.advance(0.25)
        outcome = self.outcomes[route.model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def reply(model: str) -> BackendReply:
    return BackendReply(
        text='{"blocker": "none"}',
        model=model,
        response_id=f"resp_{model}",
        usage=USAGE,
    )


def transient() -> BackendError:
    return BackendError(FailureKind.TRANSIENT, "http 503")


def make_router(
    config: RoutingConfig,
    backend: ScriptedBackend,
    clock: FakeClock,
) -> LLMRouter:
    return LLMRouter(
        config=config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
    )


async def test_first_route_wins_and_cost_is_tracked(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend({"gpt-6.1-sol": reply("gpt-6.1-sol")}, clock)
    router = make_router(routing_config, backend, clock)

    with capture_logs() as logs:
        result = await router.generate_json(request_)

    assert result.model == "gpt-6.1-sol"
    assert result.provider == "openai"
    # 1k in * $2/M + 200 out * $10/M
    assert result.cost_usd == Decimal("0.004000")
    assert result.latency_s == pytest.approx(0.25)
    assert router.budget.spent_usd == Decimal("0.004000")
    assert [a.outcome for a in result.attempts] == ["ok"]
    (event,) = logs
    assert event["event"] == "llm.call"
    assert "text" not in event
    assert request_.input_text not in str(event)


async def test_fails_over_across_generations(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend(
        {
            "gpt-6.1-sol": transient(),
            "gpt-6-sol": BackendError(FailureKind.REJECTED, "http 404"),
            "gpt-5.6-sol": reply("gpt-5.6-sol"),
        },
    )
    router = make_router(routing_config, backend, clock)

    with capture_logs() as logs:
        result = await router.generate_json(request_)

    assert backend.calls == ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"]
    assert result.model == "gpt-5.6-sol"
    assert [a.outcome for a in result.attempts] == [
        "transient",
        "rejected",
        "ok",
    ]
    failovers = [e for e in logs if e["event"] == "llm.route_failover"]
    assert len(failovers) == 2


async def test_all_routes_failing_raises_unavailable(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend(
        dict.fromkeys(
            ("gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"), transient()
        ),
    )
    router = make_router(routing_config, backend, clock)
    with pytest.raises(LLMUnavailableError) as caught:
        await router.generate_json(request_)
    assert len(caught.value.attempts) == 3


async def test_open_circuit_skips_the_model(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend(
        {"gpt-6.1-sol": transient(), "gpt-6-sol": reply("gpt-6-sol")},
    )
    router = make_router(routing_config, backend, clock)
    for _ in range(routing_config.breaker_failure_threshold):
        await router.generate_json(request_)
    backend.calls.clear()

    result = await router.generate_json(request_)

    assert backend.calls == ["gpt-6-sol"]
    assert result.attempts[0].outcome == "skipped_circuit_open"


async def test_refusal_is_not_failed_over_but_is_billed(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend(
        {"gpt-6.1-sol": LLMRefusalError("refused", USAGE)},
    )
    router = make_router(routing_config, backend, clock)
    with pytest.raises(LLMRefusalError):
        await router.generate_json(request_)
    assert backend.calls == ["gpt-6.1-sol"]
    assert router.budget.spent_usd == Decimal("0.004000")


async def test_incomplete_without_usage_consumes_conservative_reservation(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend(
        {"gpt-6.1-sol": LLMIncompleteError("max_output_tokens", None)},
    )
    router = make_router(routing_config, backend, clock)
    with pytest.raises(LLMIncompleteError):
        await router.generate_json(request_)
    assert router.budget.spent_usd > 0
    assert router.budget.reserved_usd == 0


async def test_bad_credentials_stop_the_walk(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend(
        {"gpt-6.1-sol": LLMConfigurationError("bad key")},
    )
    router = make_router(routing_config, backend, clock)
    with pytest.raises(LLMConfigurationError):
        await router.generate_json(request_)
    assert backend.calls == ["gpt-6.1-sol"]


@pytest.mark.asvs("2.4.1")
async def test_exhausted_budget_blocks_calls(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    backend = ScriptedBackend({"gpt-6.1-sol": reply("gpt-6.1-sol")})
    router = make_router(routing_config, backend, clock)
    router.budget.add(routing_config.daily_budget_usd)
    with pytest.raises(LLMBudgetExceededError):
        await router.generate_json(request_)
    assert backend.calls == []


def test_missing_backend_is_a_configuration_error(
    routing_config: RoutingConfig,
    clock: FakeClock,
) -> None:
    with pytest.raises(LLMConfigurationError, match="openai"):
        LLMRouter(
            config=routing_config,
            backends={},
            monotonic=clock.monotonic,
            today=lambda: date(2026, 10, 21),
        )


class BlockingBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def complete(
        self, request: LLMRequest, route: RouteConfig, timeout_s: float
    ) -> BackendReply:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return reply(route.model)


@pytest.mark.owasp_llm("LLM10")
async def test_concurrent_calls_reserve_budget_before_network(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    route = routing_config.routes[request_.purpose][0]
    bound = routing_config.prices[route.model].maximum_cost(
        maximum_usage(request_, max_output_tokens=route.max_output_tokens)
    )
    config = routing_config.model_copy(update={"daily_budget_usd": bound})
    backend = BlockingBackend()
    router = LLMRouter(
        config=config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
    )
    first = asyncio.create_task(router.generate_json(request_))
    await backend.started.wait()
    assert router.budget.reserved_usd == bound
    with pytest.raises(LLMBudgetExceededError):
        await router.generate_json(request_)
    assert backend.calls == 1
    backend.release.set()
    await first
    assert router.budget.reserved_usd == 0
    assert router.budget.spent_usd == Decimal("0.004000")


async def test_insufficient_nonzero_budget_rejects_before_call(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    config = routing_config.model_copy(
        update={"daily_budget_usd": Decimal("0.000001")}
    )
    backend = ScriptedBackend({})
    router = make_router(config, backend, clock)
    with pytest.raises(LLMBudgetExceededError, match="bounded"):
        await router.generate_json(request_)
    assert backend.calls == []


async def test_cancelled_half_open_probe_does_not_leak(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    backend = BlockingBackend()
    router = LLMRouter(
        config=routing_config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
    )
    route = routing_config.routes[request_.purpose][0]
    breaker = router._breakers[route.model]
    for _ in range(routing_config.breaker_failure_threshold):
        breaker.record_failure()
    clock.advance(routing_config.breaker_cooldown_s)
    probe = asyncio.create_task(router.generate_json(request_))
    await backend.started.wait()
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert router.budget.reserved_usd == 0
    assert router.budget.spent_usd > 0
    backend.release.set()
    result = await router.generate_json(request_)
    assert result.model == route.model
    assert backend.calls == 2


async def test_failover_drops_backend_error_contents(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    private_content = "owner private note bearer credential"
    backend = ScriptedBackend(
        {
            "gpt-6.1-sol": BackendError(
                FailureKind.TRANSIENT, private_content
            ),
            "gpt-6-sol": reply("gpt-6-sol"),
        }
    )
    with capture_logs() as logs:
        await make_router(routing_config, backend, clock).generate_json(
            request_
        )
    assert private_content not in str(logs)


async def test_missing_usage_settles_full_conservative_price(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    route = routing_config.routes[request_.purpose][0]
    bound = maximum_usage(request_, max_output_tokens=route.max_output_tokens)
    backend = ScriptedBackend(
        {
            route.model: BackendReply(
                text="{}",
                model=route.model,
                response_id="stub",
                usage=bound,
                usage_reported=False,
            )
        }
    )
    router = make_router(routing_config, backend, clock)
    result = await router.generate_json(request_)
    assert result.cost_usd == routing_config.prices[route.model].maximum_cost(
        bound
    )
    assert router.budget.reserved_usd == 0


async def test_provider_overrun_is_charged_and_prevents_more_calls(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    route = routing_config.routes[request_.purpose][0]
    backend = ScriptedBackend(
        {
            route.model: BackendReply(
                text="{}",
                model=route.model,
                response_id="stub",
                usage=TokenUsage(1_000_000, 200),
            )
        }
    )
    router = make_router(routing_config, backend, clock)
    result = await router.generate_json(request_)
    assert router.budget.spent_usd == result.cost_usd
    assert router.budget.overage_usd > 0
    with pytest.raises(LLMBudgetExceededError):
        await router.generate_json(request_)
    assert backend.calls == [route.model]


class InterleavedBackend:
    def __init__(self) -> None:
        self.started = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
        self.calls = 0

    async def complete(
        self, request: LLMRequest, route: RouteConfig, timeout_s: float
    ) -> BackendReply:
        index = self.calls
        self.calls += 1
        self.started[index].set()
        if index == 1:
            raise transient()
        await self.release[index].wait()
        return reply(route.model)


def one_route(config: RoutingConfig, **updates: object) -> RoutingConfig:
    return config.model_copy(
        update={
            "routes": {
                purpose: routes[:1]
                for purpose, routes in config.routes.items()
            },
            "breaker_failure_threshold": 1,
            **updates,
        }
    )


async def test_cancelled_old_request_cannot_release_new_router_probe(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    config = one_route(routing_config)
    backend = InterleavedBackend()
    router = LLMRouter(
        config=config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
    )
    stale = asyncio.create_task(router.generate_json(request_))
    await backend.started[0].wait()
    with pytest.raises(LLMUnavailableError):
        await router.generate_json(request_)
    clock.advance(config.breaker_cooldown_s)
    probe = asyncio.create_task(router.generate_json(request_))
    await backend.started[2].wait()
    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale
    with pytest.raises(LLMUnavailableError):
        await router.generate_json(request_)
    assert backend.calls == 3
    backend.release[2].set()
    assert (await probe).text


async def test_total_router_deadline_includes_waiting_for_semaphore(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    backend = BlockingBackend()
    router = LLMRouter(
        config=one_route(routing_config, request_timeout_s=0.001),
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
    )
    for _ in range(4):
        await router._semaphore.acquire()
    with pytest.raises(LLMUnavailableError) as caught:
        await router.generate_json(request_)
    assert backend.calls == 0
    assert caught.value.attempts[0].outcome == "queue_timeout"
    assert router.budget.reserved_usd == 0
    assert router.budget.spent_usd == 0


class SharedLedger:
    """An in-memory stand-in for the PostgreSQL spend ledger."""

    def __init__(self, limit: Decimal, *, failing: bool = False) -> None:
        self.limit = limit
        self.failing = failing
        self.spent = Decimal(0)
        self.reserved: dict[UUID, Decimal] = {}
        self.settled: list[Decimal] = []

    async def reserve(self, amount_usd: Decimal) -> UUID | None:
        if self.failing:
            message = "ledger down"
            raise RuntimeError(message)
        if self.spent + sum(self.reserved.values()) + amount_usd > self.limit:
            return None
        ticket = uuid4()
        self.reserved[ticket] = amount_usd
        return ticket

    async def settle(self, ticket: UUID, cost_usd: Decimal) -> None:
        self.reserved.pop(ticket)
        self.spent += cost_usd
        self.settled.append(cost_usd)


def bounded(
    routing_config: RoutingConfig, request_: LLMRequest
) -> tuple[RoutingConfig, Decimal]:
    route = routing_config.routes[request_.purpose][0]
    bound = routing_config.prices[route.model].maximum_cost(
        maximum_usage(request_, max_output_tokens=route.max_output_tokens)
    )
    return routing_config, bound


@pytest.mark.owasp_llm("LLM10")
async def test_workers_share_one_daily_budget(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    config, bound = bounded(routing_config, request_)
    ledger = SharedLedger(bound)
    first_backend, second_backend = BlockingBackend(), ScriptedBackend({})
    first, second = (
        LLMRouter(
            config=config,
            backends={"openai": backend},
            monotonic=clock.monotonic,
            today=lambda: date(2026, 10, 21),
            ledger=ledger,
        )
        for backend in (first_backend, second_backend)
    )
    running = asyncio.create_task(first.generate_json(request_))
    await first_backend.started.wait()
    # Another process has its own budget but the same ledger.
    with pytest.raises(LLMBudgetExceededError, match="shared"):
        await second.generate_json(request_)
    assert second_backend.calls == []
    assert second.budget.reserved_usd == 0
    first_backend.release.set()
    await running
    assert ledger.settled == [Decimal("0.004000")]
    assert ledger.reserved == {}


async def test_unknown_bill_is_charged_at_the_bound_in_the_ledger(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    config, bound = bounded(routing_config, request_)
    ledger = SharedLedger(bound * 2)
    router = LLMRouter(
        config=config,
        backends={
            "openai": ScriptedBackend(
                {"gpt-6.1-sol": LLMIncompleteError("cut", usage=None)}
            )
        },
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
        ledger=ledger,
    )
    with pytest.raises(LLMIncompleteError):
        await router.generate_json(request_)
    assert ledger.settled == [bound]


async def test_unreachable_ledger_fails_closed_and_releases_admission(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    backend = ScriptedBackend({"gpt-6.1-sol": reply("gpt-6.1-sol")})
    router = LLMRouter(
        config=routing_config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
        ledger=SharedLedger(Decimal(5), failing=True),
    )
    with pytest.raises(RuntimeError, match="ledger down"):
        await router.generate_json(request_)
    assert backend.calls == []
    assert router.budget.reserved_usd == 0
    breaker = router.breakers["gpt-6.1-sol"]
    assert breaker.admit() is not None


@pytest.mark.parametrize("cancel", [False, True])
async def test_queue_only_attempts_release_both_budgets_and_keep_model_healthy(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
    cancel: bool,
) -> None:
    config = one_route(
        routing_config,
        request_timeout_s=1 if cancel else 0.001,
        breaker_failure_threshold=1,
    )
    backend = ScriptedBackend({"gpt-6.1-sol": reply("gpt-6.1-sol")})
    ledger = SharedLedger(Decimal(5))
    router = LLMRouter(
        config=config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
        ledger=ledger,
    )
    for _ in range(4):
        await router._semaphore.acquire()
    if cancel:
        pending = asyncio.create_task(router.generate_json(request_))
        await asyncio.sleep(0)
        assert len(ledger.reserved) == 1
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        with pytest.raises(LLMUnavailableError):
            await router.generate_json(request_)
    assert backend.calls == []
    assert router.budget.spent_usd == 0
    assert router.budget.reserved_usd == 0
    assert ledger.settled == [Decimal(0)]
    assert ledger.reserved == {}
    assert router.breakers["gpt-6.1-sol"].state == "closed"
    for _ in range(4):
        router._semaphore.release()
    assert (await router.generate_json(request_)).model == "gpt-6.1-sol"
    assert backend.calls == ["gpt-6.1-sol"]
    assert ledger.spent == Decimal("0.004000")


async def test_entered_provider_timeout_keeps_unknown_bill_and_opens_breaker(
    routing_config: RoutingConfig, request_: LLMRequest, clock: FakeClock
) -> None:
    config = one_route(
        routing_config, request_timeout_s=0.001, breaker_failure_threshold=1
    )
    _, bound = bounded(config, request_)
    ledger = SharedLedger(bound * 2)
    backend = BlockingBackend()
    router = LLMRouter(
        config=config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
        ledger=ledger,
    )
    with pytest.raises(LLMUnavailableError) as caught:
        await router.generate_json(request_)
    assert backend.calls == 1
    assert caught.value.attempts[0].outcome == "timeout"
    assert router.budget.spent_usd == bound
    assert router.budget.reserved_usd == 0
    assert ledger.spent == bound
    assert ledger.settled == [bound]
    assert ledger.reserved == {}
    assert router.breakers["gpt-6.1-sol"].state == "open"


@pytest.mark.parametrize("unbilled", [False, True])
async def test_provider_error_status_is_not_charged_to_either_budget(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
    unbilled: bool,
) -> None:
    config, bound = bounded(routing_config, request_)
    config = one_route(config)
    route = config.routes[request_.purpose][0]
    ledger = SharedLedger(bound)
    backend = ScriptedBackend(
        {
            route.model: BackendError(
                FailureKind.TRANSIENT, "http 429", unbilled=unbilled
            )
        }
    )
    router = LLMRouter(
        config=config,
        backends={"openai": backend},
        monotonic=clock.monotonic,
        today=lambda: date(2026, 10, 21),
        ledger=ledger,
    )
    with pytest.raises(LLMUnavailableError):
        await router.generate_json(request_)
    expected = Decimal(0) if unbilled else bound
    assert router.budget.spent_usd == expected
    assert router.budget.reserved_usd == 0
    assert ledger.settled == [expected]
    # A refusal by status still counts against the model's health.
    assert router.breakers[route.model].state == "open"
