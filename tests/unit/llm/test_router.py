from datetime import date
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from llm_doubles import FakeClock
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


async def test_incomplete_without_usage_is_not_billed(
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
    assert router.budget.spent_usd == 0


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
