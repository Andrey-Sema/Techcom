"""Validated routing configuration for the LLM router (``llm.toml``)."""

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from techcom_followup.adapters.outbound.llm.catalog import (
    KNOWN_MODELS,
    ModelPrice,
    ReasoningEffort,
    Verbosity,
)
from techcom_followup.application.ports import LLMPurpose

type ServiceTier = Literal["auto", "default", "flex", "priority"]


class RouteConfig(BaseModel):
    """One candidate model for a purpose, tried in list order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    effort: ReasoningEffort = "low"
    verbosity: Verbosity = "low"
    max_output_tokens: int = Field(default=2048, ge=64, le=32_768)
    service_tier: ServiceTier = "auto"

    @property
    def provider(self) -> str:
        """Provider that serves this model (from the catalogue)."""
        return KNOWN_MODELS[self.model].provider


class RoutingConfig(BaseModel):
    """Complete router configuration, checked against the catalogue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    routes: dict[LLMPurpose, tuple[RouteConfig, ...]]
    prices: dict[str, ModelPrice]
    daily_budget_usd: Decimal = Field(ge=0)
    request_timeout_s: float = Field(default=20.0, gt=0, le=120)
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_cooldown_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _check_routes(self) -> Self:
        missing = set(LLMPurpose) - set(self.routes)
        if missing:
            names = ", ".join(sorted(missing))
            msg = f"no routes configured for: {names}"
            raise ValueError(msg)
        for purpose, routes in self.routes.items():
            if not routes:
                msg = f"route list for {purpose} is empty"
                raise ValueError(msg)
            for route in routes:
                _check_route(route, self.prices)
        return self


def _check_route(route: RouteConfig, prices: dict[str, ModelPrice]) -> None:
    capabilities = KNOWN_MODELS.get(route.model)
    if capabilities is None:
        msg = f"unknown model {route.model!r}; add it to the catalogue first"
        raise ValueError(msg)
    if route.effort not in capabilities.efforts:
        msg = f"{route.model} does not support effort {route.effort!r}"
        raise ValueError(msg)
    if not capabilities.structured_outputs:
        msg = f"{route.model} has no structured outputs"
        raise ValueError(msg)
    if route.max_output_tokens > capabilities.max_output_tokens:
        msg = f"max_output_tokens too large for {route.model}"
        raise ValueError(msg)
    if route.model not in prices:
        msg = f"no price configured for {route.model}"
        raise ValueError(msg)


def load_routing_config(path: Path) -> RoutingConfig:
    """Read and validate a TOML routing file.

    Raises:
        OSError: If the file cannot be read.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        pydantic.ValidationError: If the content violates the schema.
    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return RoutingConfig.model_validate(raw)
