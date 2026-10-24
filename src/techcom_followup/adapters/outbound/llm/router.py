"""Model router: ordered failover with circuit breakers and a budget.

For each purpose the router walks its route list in order (for
example ``gpt-6.1-sol`` → ``gpt-6-sol`` → ``gpt-5.6-sol``). It fails
over only on *availability* problems (timeouts, 429, 5xx, model not
available). A refusal or an incomplete answer is returned to the
caller unchanged: a second model would cost money without making the
deterministic fallback any safer. Credential errors stop the walk
immediately because every route of a provider shares the credentials.
"""

from asyncio import Semaphore, shield, timeout
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import structlog

from techcom_followup.adapters.outbound.llm.accounting import maximum_usage
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
from techcom_followup.application.resilience_ports import BreakerPermit
from techcom_followup.application.telemetry import (
    NULL_TELEMETRY,
    CounterName,
    HistogramName,
    Telemetry,
)
from techcom_followup.infrastructure.resilience import (
    CircuitBreaker,
    DailySpendBudget,
    SpendLedger,
    SpendReservation,
)

_log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Admission:
    """Both budgets and the provider-start boundary of one attempt."""

    bound: Decimal
    reservation: SpendReservation
    ticket: UUID | None
    started: bool = False
    # The provider refused with an error status: nothing was generated.
    unbilled: bool = False

    def settlement(self, cost: Decimal | None) -> Decimal | None:
        """Charge nothing unless the provider may have generated."""
        if not self.started or self.unbilled:
            return Decimal(0)
        return cost


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
        telemetry: Telemetry = NULL_TELEMETRY,
        ledger: SpendLedger | None = None,
    ) -> None:
        """Wire routes, breakers (one per model) and the daily budget.

        With a shared ``ledger`` every attempt is admitted by both the
        process budget and the budget shared by all workers.

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
        self._telemetry = telemetry
        self._ledger = ledger
        self._backends = dict(backends)
        self._semaphore = Semaphore(4)
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
    def breakers(self) -> Mapping[str, CircuitBreaker]:
        """Expose per-model breakers for state metrics."""
        return self._breakers

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
        permit = breaker.admit()
        if permit is None:
            attempts.append(_attempt(route, "skipped_circuit_open"))
            return None
        admission = await self._admit(request, route, breaker, permit)
        cost: Decimal | None = None
        try:
            async with timeout(self._config.request_timeout_s):
                result = await self._reserved_route(
                    request, route, attempts, permit, admission
                )
            if result is not None:
                cost = result.cost_usd
        except TimeoutError:
            if admission.started:
                breaker.record_failure(permit)
            attempts.append(
                _attempt(
                    route, "timeout" if admission.started else "queue_timeout"
                )
            )
            return None
        except LLMBilledError as exc:
            if exc.usage is not None:
                cost = self._price(route).cost(exc.usage)
            raise
        else:
            return result
        finally:
            breaker.abandon(permit)
            await self._release(admission, admission.settlement(cost), request)

    async def _admit(
        self,
        request: LLMRequest,
        route: RouteConfig,
        breaker: CircuitBreaker,
        permit: BreakerPermit,
    ) -> _Admission:
        """Reserve the worst-case cost locally, then in the shared one.

        Raises:
            LLMBudgetExceededError: Either budget refused the attempt.
        """
        bound = self._price(route).maximum_cost(
            maximum_usage(request, max_output_tokens=route.max_output_tokens)
        )
        reservation = self._budget.reserve(bound)
        if reservation is None:
            breaker.abandon(permit)
            message = "insufficient daily LLM budget for bounded attempt"
            raise LLMBudgetExceededError(message)
        try:
            ticket = await self._reserve_shared(bound, request)
        except BaseException:
            # No provider call happened: release both admissions.
            breaker.abandon(permit)
            self._budget.settle(reservation, Decimal(0))
            raise
        return _Admission(bound, reservation, ticket)

    async def _release(
        self,
        admission: _Admission,
        cost: Decimal | None,
        request: LLMRequest,
    ) -> None:
        """Settle both budgets; an unknown bill costs the full bound."""
        self._settle(admission.reservation, cost, request)
        if self._ledger is not None and admission.ticket is not None:
            # Shielded: a cancelled call may still have been billed.
            await shield(
                self._ledger.settle(
                    admission.ticket,
                    admission.bound if cost is None else cost,
                )
            )

    async def _reserve_shared(
        self, bound: Decimal, request: LLMRequest
    ) -> UUID | None:
        """Admit the attempt against the budget shared by all workers.

        Raises:
            LLMBudgetExceededError: The shared day is spent or the
                ledger cannot be reached (fail closed).
        """
        if self._ledger is None:
            return None
        ticket = await self._ledger.reserve(bound)
        if ticket is None:
            _log.warning("llm.budget_exhausted", purpose=request.purpose)
            message = "shared daily LLM budget exhausted"
            raise LLMBudgetExceededError(message)
        return ticket

    def _settle(
        self,
        reservation: SpendReservation,
        cost: Decimal | None,
        request: LLMRequest,
    ) -> None:
        self._budget.settle(reservation, cost)
        if cost is not None:
            self._telemetry.count(
                CounterName.LLM_COST,
                float(cost),
                purpose=request.purpose.value,
            )

    async def _reserved_route(
        self,
        request: LLMRequest,
        route: RouteConfig,
        attempts: list[RouteAttempt],
        permit: BreakerPermit,
        admission: _Admission,
    ) -> LLMResult | None:
        breaker = self._breakers[route.model]
        started = self._monotonic()
        backend = self._backends[route.provider]
        try:
            async with self._semaphore:
                admission.started = True
                reply = await backend.complete(
                    request,
                    route,
                    self._config.request_timeout_s,
                )
        except BackendError as exc:
            admission.unbilled = exc.unbilled
            breaker.record_failure(permit)
            attempts.append(_attempt(route, str(exc.kind)))
            _log.warning(
                "llm.route_failover",
                purpose=request.purpose,
                model=route.model,
                kind=str(exc.kind),
            )
            return None
        except LLMConfigurationError:
            breaker.record_failure(permit)
            raise
        except LLMBilledError:
            # The model answered, so the route is healthy; bill tokens.
            breaker.record_success(permit)
            raise
        breaker.record_success(permit)
        return self._success(request, route, reply, started, attempts)

    def _success(
        self,
        request: LLMRequest,
        route: RouteConfig,
        reply: BackendReply,
        started: float,
        attempts: list[RouteAttempt],
    ) -> LLMResult:
        price = self._price(route)
        cost = (
            price.cost(reply.usage)
            if reply.usage_reported
            else price.maximum_cost(reply.usage)
        )
        attempts.append(_attempt(route, "ok"))
        latency = self._monotonic() - started
        purpose = request.purpose.value
        self._telemetry.observe(
            HistogramName.LLM_LATENCY, latency, purpose=purpose
        )
        for direction, tokens in (
            ("input", reply.usage.input_tokens),
            ("output", reply.usage.output_tokens),
        ):
            self._telemetry.count(
                CounterName.LLM_TOKENS,
                tokens,
                purpose=purpose,
                direction=direction,
            )
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
