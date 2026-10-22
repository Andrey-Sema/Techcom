"""Model router: ordered failover with circuit breakers and a budget.

For each purpose the router walks its route list in order (for
example ``gpt-6.1-sol`` → ``gpt-6-sol`` → ``gpt-5.6-sol``). It fails
over only on *availability* problems (timeouts, 429, 5xx, model not
available). A refusal or an incomplete answer is returned to the
caller unchanged: a second model would cost money without making the
deterministic fallback any safer. Credential errors stop the walk
immediately because every route of a provider shares the credentials.
"""

from collections.abc import Callable, Mapping
from datetime import date
from typing import Protocol

import structlog

from techcom_followup.adapters.outbound.llm.catalog import ModelPrice
from techcom_followup.adapters.outbound.llm.config import (
    RouteConfig,
    RoutingConfig,
)
from techcom_followup.adapters.outbound.llm.openai_responses import (
    BackendError,
    BackendReply,
)
from techcom_followup.application.ports import (
    LLMBilledError,
    LLMBudgetExceededError,
    LLMConfigurationError,
    LLMRequest,
    LLMResult,
    LLMUnavailableError,
    RouteAttempt,
)
from techcom_followup.infrastructure.resilience import (
    CircuitBreaker,
    DailySpendBudget,
)

_log = structlog.get_logger(__name__)


class LLMBackend(Protocol):
    """A provider backend that can run one route."""

    async def complete(
        self,
        request: LLMRequest,
        route: RouteConfig,
        timeout_s: float,
    ) -> BackendReply:
        """Run the request against the route's model."""
        ...


class LLMRouter:
    """Implements the ``LLMClient`` port on top of provider backends."""

    def __init__(
        self,
        *,
        config: RoutingConfig,
        backends: Mapping[str, LLMBackend],
        monotonic: Callable[[], float],
        today: Callable[[], date],
    ) -> None:
        """Wire routes, breakers (one per model) and the daily budget.

        Raises:
            LLMConfigurationError: A route needs a backend that is not
                provided.
        """
        providers = {
            route.provider
            for routes in config.routes.values()
            for route in routes
        }
        missing = providers - set(backends)
        if missing:
            msg = f"no backend for providers: {', '.join(sorted(missing))}"
            raise LLMConfigurationError(msg)
        self._config = config
        self._backends = dict(backends)
        self._monotonic = monotonic
        self._budget = DailySpendBudget(
            limit_usd=config.daily_budget_usd,
            today=today,
        )
        self._breakers = {
            route.model: CircuitBreaker(
                failure_threshold=config.breaker_failure_threshold,
                cooldown_s=config.breaker_cooldown_s,
                monotonic=monotonic,
            )
            for routes in config.routes.values()
            for route in routes
        }

    @property
    def budget(self) -> DailySpendBudget:
        """Expose the budget for metrics and tests."""
        return self._budget

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        """Return the first successful route's output.

        Raises:
            LLMBudgetExceededError: Today's budget is spent.
            LLMConfigurationError: Credentials are invalid.
            LLMBilledError: Refusal or incomplete output (not retried).
            LLMUnavailableError: Every route failed or was skipped.
        """
        if self._budget.exhausted():
            _log.warning("llm.budget_exhausted", purpose=request.purpose)
            msg = "daily LLM budget exhausted"
            raise LLMBudgetExceededError(msg)
        attempts: list[RouteAttempt] = []
        for route in self._config.routes[request.purpose]:
            result = await self._try_route(request, route, attempts)
            if result is not None:
                return result
        _log.error(
            "llm.unavailable",
            purpose=request.purpose,
            attempts=[f"{a.model}:{a.outcome}" for a in attempts],
        )
        raise LLMUnavailableError(tuple(attempts))

    async def _try_route(
        self,
        request: LLMRequest,
        route: RouteConfig,
        attempts: list[RouteAttempt],
    ) -> LLMResult | None:
        breaker = self._breakers[route.model]
        if not breaker.allow():
            attempts.append(_attempt(route, "skipped_circuit_open"))
            return None
        started = self._monotonic()
        backend = self._backends[route.provider]
        try:
            reply = await backend.complete(
                request,
                route,
                self._config.request_timeout_s,
            )
        except BackendError as exc:
            breaker.record_failure()
            attempts.append(_attempt(route, str(exc.kind)))
            _log.warning(
                "llm.route_failover",
                purpose=request.purpose,
                model=route.model,
                kind=str(exc.kind),
                detail=exc.detail,
            )
            return None
        except LLMConfigurationError:
            breaker.record_failure()
            raise
        except LLMBilledError as exc:
            # The model answered, so the route is healthy; bill tokens.
            breaker.record_success()
            if exc.usage is not None:
                self._budget.add(self._price(route).cost(exc.usage))
            raise
        breaker.record_success()
        return self._success(request, route, reply, started, attempts)

    def _success(
        self,
        request: LLMRequest,
        route: RouteConfig,
        reply: BackendReply,
        started: float,
        attempts: list[RouteAttempt],
    ) -> LLMResult:
        cost = self._price(route).cost(reply.usage)
        self._budget.add(cost)
        attempts.append(_attempt(route, "ok"))
        latency = self._monotonic() - started
        _log.info(
            "llm.call",
            purpose=request.purpose,
            prompt_version=request.prompt_version,
            model=reply.model,
            response_id=reply.response_id,
            tokens_in=reply.usage.input_tokens,
            tokens_out=reply.usage.output_tokens,
            tokens_cached=reply.usage.cached_input_tokens,
            tokens_reasoning=reply.usage.reasoning_tokens,
            cost_usd=str(cost),
            latency_s=round(latency, 3),
            attempt_count=len(attempts),
        )
        return LLMResult(
            text=reply.text,
            provider=route.provider,
            model=reply.model,
            response_id=reply.response_id,
            usage=reply.usage,
            cost_usd=cost,
            latency_s=latency,
            attempts=tuple(attempts),
        )

    def _price(self, route: RouteConfig) -> ModelPrice:
        # Prices are validated at config load for every routed model.
        return self._config.prices[route.model]


def _attempt(route: RouteConfig, outcome: str) -> RouteAttempt:
    return RouteAttempt(
        provider=route.provider,
        model=route.model,
        outcome=outcome,
    )
