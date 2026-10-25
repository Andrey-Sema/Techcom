"""Bind captured provider responses to their exact requests."""

from openai.types.responses import Response

from eval.contracts import (
    ProviderEvidence,
    Recording,
    Source,
    digest_json,
    digest_text,
)
from techcom_followup.application.ports import LLMRequest

ENDPOINT = "https://api.openai.com/v1/responses"


def recording_from_capture(
    case_id: str,
    request: LLMRequest,
    response: Response,
    *,
    captured_at: str,
    attestation: str,
) -> Recording:
    """Keep the raw response next to the exact request bindings."""
    raw = response.model_dump(mode="json", by_alias=True)
    for item in raw["output"]:
        # Encrypted reasoning is opaque, bound to the capturing
        # organisation and not needed to replay the answer text.
        if item.get("type") == "reasoning":
            item["encrypted_content"] = None
    return Recording(
        case_id=case_id,
        purpose=request.purpose,
        prompt_version=request.prompt_version,
        schema_name=request.schema_name,
        schema_sha256=digest_json(dict(request.json_schema)),
        instructions_sha256=digest_text(request.instructions),
        input_sha256=digest_text(request.input_text),
        source=Source.PROVIDER,
        model=response.model,
        response_id=response.id,
        text=response.output_text,
        provider_evidence=ProviderEvidence(
            endpoint=ENDPOINT,
            captured_at=captured_at,
            operator_attestation=attestation,
            raw_response=raw,
        ),
    )
