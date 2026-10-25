"""Catalogue of supported models, their capabilities and prices.

Only models that the router is allowed to use are listed. A model id
that is not in the catalogue is rejected at start-up, so a typo in the
configuration fails fast instead of failing on the first request.

Capability data was checked against the installed ``openai`` SDK
(3.36.x lists these model ids) and the provider's model pages; prices
are configuration (``config/llm.toml``) because they change.
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from techcom_followup.application.ports import TokenUsage

type ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]
type Verbosity = Literal["low", "medium", "high"]

_MILLION = Decimal(1_000_000)
_SIX_PLACES = Decimal("0.000001")

# The Sol generation rejects "none" and "minimal": reasoning cannot be
# switched off, only reduced to "low".
_SOL_EFFORTS: frozenset[ReasoningEffort] = frozenset(
    {"low", "medium", "high", "xhigh", "max"},
)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What the router may ask of a model."""

    provider: str
    efforts: frozenset[ReasoningEffort]
    structured_outputs: bool
    max_output_tokens: int


KNOWN_MODELS: dict[str, ModelCapabilities] = {
    "gpt-6-luna": ModelCapabilities(
        provider="openai",
        efforts=_SOL_EFFORTS,
        structured_outputs=True,
        max_output_tokens=128_000,
    ),
    "gpt-6.1-sol": ModelCapabilities(
        provider="openai",
        efforts=_SOL_EFFORTS,
        structured_outputs=True,
        max_output_tokens=128_000,
    ),
    "gpt-6-sol": ModelCapabilities(
        provider="openai",
        efforts=_SOL_EFFORTS,
        structured_outputs=True,
        max_output_tokens=128_000,
    ),
    "gpt-5.6-sol": ModelCapabilities(
        provider="openai",
        efforts=_SOL_EFFORTS,
        structured_outputs=True,
        max_output_tokens=128_000,
    ),
}


class ModelPrice(BaseModel):
    """Price list for one model, in USD per million tokens.

    ``cached_input_per_mtok`` may be unknown; the input price is then
    used for cached tokens, which over-estimates cost (safe for
    budgets).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cached_input_per_mtok: Decimal | None = Field(default=None, ge=0)
    cache_write_multiplier: Decimal = Field(default=Decimal("1.25"), ge=1)
    long_context_threshold: int | None = Field(default=272_000, gt=0)
    long_context_input_multiplier: Decimal = Field(default=Decimal(2), ge=1)
    long_context_output_multiplier: Decimal = Field(
        default=Decimal("1.5"),
        ge=1,
    )
    source: str = Field(min_length=1)
    verified_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    def cost(self, usage: TokenUsage) -> Decimal:
        """Return the USD cost of one response, rounded to 6 places.

        Reasoning tokens are part of ``output_tokens`` and billed as
        output. Prompts above the long-context threshold are billed at
        the long-context multipliers for the whole request.
        """
        long_context = (
            self.long_context_threshold is not None
            and usage.input_tokens > self.long_context_threshold
        )
        input_multiplier = (
            self.long_context_input_multiplier if long_context else Decimal(1)
        )
        output_multiplier = (
            self.long_context_output_multiplier if long_context else Decimal(1)
        )
        cached_price = (
            self.input_per_mtok
            if self.cached_input_per_mtok is None
            else self.cached_input_per_mtok
        )
        uncached = max(
            usage.input_tokens
            - usage.cached_input_tokens
            - usage.cache_write_tokens,
            0,
        )
        input_cost = (
            uncached * self.input_per_mtok
            + usage.cached_input_tokens * cached_price
            + usage.cache_write_tokens
            * self.input_per_mtok
            * self.cache_write_multiplier
        ) * input_multiplier
        output_cost = (
            usage.output_tokens * self.output_per_mtok * output_multiplier
        )
        return ((input_cost + output_cost) / _MILLION).quantize(_SIX_PLACES)

    def maximum_cost(self, usage: TokenUsage) -> Decimal:
        """Bound cached and cache-write mixes under this price table."""
        return max(
            self.cost(usage),
            self.cost(replace(usage, cached_input_tokens=usage.input_tokens)),
            self.cost(replace(usage, cache_write_tokens=usage.input_tokens)),
        )
