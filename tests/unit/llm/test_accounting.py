import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from techcom_followup.adapters.outbound.llm.accounting import (
    PROTOCOL_TOKEN_ALLOWANCE,
    maximum_usage,
)
from techcom_followup.adapters.outbound.llm.catalog import ModelPrice
from techcom_followup.application.ports import (
    LLMPurpose,
    LLMRequest,
    TokenUsage,
)


@given(st.text())
def test_input_bound_covers_utf8_unicode_and_schema_bytes(text: str) -> None:
    request = LLMRequest(
        purpose=LLMPurpose.INTERPRET_NOTE,
        instructions=text,
        input_text=text,
        json_schema={"description": text},
        schema_name="bounded",
        prompt_version="v1",
    )
    usage = maximum_usage(request, max_output_tokens=2048)
    raw_bytes = len(text.encode("utf-8", "surrogatepass"))
    assert usage.input_tokens >= 3 * raw_bytes + PROTOCOL_TOKEN_ALLOWANCE
    assert usage.output_tokens == 2048


@pytest.mark.parametrize("cached_price", [Decimal(100), Decimal("0.1"), None])
def test_reservation_price_covers_all_token_mix_and_long_context(
    cached_price: Decimal | None,
) -> None:
    price = ModelPrice(
        input_per_mtok=Decimal(2),
        output_per_mtok=Decimal(10),
        cached_input_per_mtok=cached_price,
        long_context_threshold=100,
        source="test",
        verified_on="2026-10-21",
    )
    usage = TokenUsage(1000, 2048)
    bound = price.maximum_cost(usage)
    for cached in range(0, 1001, 100):
        for write in range(0, 1001 - cached, 100):
            actual = dataclasses.replace(
                usage, cached_input_tokens=cached, cache_write_tokens=write
            )
            assert price.cost(actual) <= bound
