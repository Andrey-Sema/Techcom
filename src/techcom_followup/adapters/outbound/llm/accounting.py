"""Conservative token bounds for reservation and missing usage.

Byte-level tokenizers consume at least one UTF-8 byte per content token.
ASCII-escaped JSON is at least as long as the UTF-8 content, including
supplementary Unicode. Count the input and schema, then add 8,192 tokens
for provider framing, request settings and schema compilation.

The framing allowance is an explicit backend assumption requiring review
on model changes. Arbitrary provider overhead cannot be proven locally.
Reported overruns are fully charged and permanently block admission.
Provider-side spend quotas remain the ultimate external hard cap;
this process cannot enforce arbitrary hidden overhead or shared workers.
"""

import json

from techcom_followup.application.ports import LLMRequest, TokenUsage

PROTOCOL_TOKEN_ALLOWANCE = 8192


def maximum_usage(
    request: LLMRequest, *, max_output_tokens: int
) -> TokenUsage:
    """Bound input and schema tokens; reserve the full output cap."""
    serialized = json.dumps(
        {
            "instructions": request.instructions,
            "input": request.input_text,
            "schema_name": request.schema_name,
            "schema": dict(request.json_schema),
            "prompt_version": request.prompt_version,
            "safety_identifier": request.safety_identifier,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return TokenUsage(
        input_tokens=len(serialized.encode("utf-8"))
        + PROTOCOL_TOKEN_ALLOWANCE,
        output_tokens=max_output_tokens,
    )
