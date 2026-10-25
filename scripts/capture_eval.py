r"""Capture live provider responses for the evaluation corpus.

Runs every case of a bundle through the same interpreter, policy and
drafter as the replay harness, against one real model, and writes a
provider bundle that ``python -m eval --require-provider`` replays
offline. Nothing is written unless every request got a response.

Usage (needs ``FOLLOWUP_OPENAI_API_KEY``)::

    PYTHONPATH=tests uv run --locked python scripts/capture_eval.py \
      --model gpt-6-luna --effort high --out tests/eval/bundles/NAME \
      --attestation "who captured it, where and when"
"""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from openai import AsyncOpenAI
from openai.types.responses import Response

from eval import harness
from eval.contracts import EvalCase, Manifest, Recording, digest_text
from eval.provider import recording_from_capture
from techcom_followup.adapters.outbound.llm.config import (
    RouteConfig,
    load_routing_config,
)
from techcom_followup.adapters.outbound.llm.factory import build_openai_client
from techcom_followup.adapters.outbound.llm.openai_responses import (
    BackendError,
    FailureKind,
    OpenAIResponsesBackend,
)
from techcom_followup.application.drafter import (
    PROMPT_VERSION as DRAFT_PROMPT,
)
from techcom_followup.application.drafter import MessageDrafter
from techcom_followup.application.interpreter import (
    PROMPT_VERSION as INTERPRET_PROMPT,
)
from techcom_followup.application.interpreter import NoteInterpreter
from techcom_followup.application.llm_workflow import LanguageCall
from techcom_followup.application.ports import (
    LLMError,
    LLMPurpose,
    LLMRequest,
    LLMResult,
)
from techcom_followup.domain.injection import screen
from techcom_followup.infrastructure.settings import load_openai_settings

SOURCE = Path("tests/eval/bundles/hardening.v2")
CALL_TIMEOUT_S = 60.0
ATTEMPTS = 3


class CaptureError(Exception):
    """A request got no response; the bundle would be incomplete."""


class CaptureClient:
    """Call one model and keep every raw response for the bundle."""

    def __init__(
        self,
        client: AsyncOpenAI,
        routes: dict[LLMPurpose, RouteConfig],
        case_id: str,
        attestation: str,
    ) -> None:
        """Remember where to send requests and how to label them."""
        self._client = client
        self._routes = routes
        self._case_id = case_id
        self._attestation = attestation
        self.recordings: list[Recording] = []

    async def generate_json(self, request: LLMRequest) -> LLMResult:
        """Send one request, retrying only transient failures."""
        raw: list[Response] = []

        async def create(**kwargs: object) -> Response:
            response = await self._client.responses.create(**kwargs)
            raw.append(response)
            return response

        backend = OpenAIResponsesBackend(create)
        started = time.monotonic()
        for attempt in range(ATTEMPTS):
            raw.clear()
            try:
                reply = await backend.complete(
                    request,
                    self._routes[request.purpose],
                    timeout_s=CALL_TIMEOUT_S,
                )
            except BackendError as error:
                if error.kind is not FailureKind.TRANSIENT:
                    raise CaptureError(str(error)) from error
                await asyncio.sleep(2**attempt)
                continue
            except LLMError as error:
                raise CaptureError(type(error).__name__) from error
            self.recordings.append(
                recording_from_capture(
                    self._case_id,
                    request,
                    raw[-1],
                    captured_at=datetime.now(UTC).isoformat(),
                    attestation=self._attestation,
                )
            )
            return LLMResult(
                text=reply.text,
                provider="openai",
                model=reply.model,
                response_id=reply.response_id,
                usage=reply.usage,
                cost_usd=Decimal(0),
                latency_s=time.monotonic() - started,
            )
        message = f"{self._case_id}: no response after {ATTEMPTS} attempts"
        raise CaptureError(message)


async def capture_case(
    case: EvalCase, client: CaptureClient
) -> tuple[Recording, ...]:
    """Run one case exactly as the replay harness does."""
    task = harness.task_for(case)
    screened = screen(task.notes)
    interpreter = NoteInterpreter(
        client, model="evaluation.replay", now=lambda: harness.NOW
    )
    interpreter.calls = LanguageCall(
        client, monotonic=lambda: 0, timeout_s=CALL_TIMEOUT_S * ATTEMPTS
    )
    drafter = MessageDrafter(client)
    drafter.calls = LanguageCall(client, timeout_s=CALL_TIMEOUT_S * ATTEMPTS)
    interpretation = await interpreter.interpret(
        task, as_of=harness.AS_OF, screen=screened
    )
    decision = harness._POLICY.decide(  # noqa: SLF001
        task,
        as_of=harness.AS_OF,
        screen=screened,
        interpretation=interpretation,
    )
    if decision.actionable:
        await drafter.draft(decision, task, as_of=harness.AS_OF)
    lost = interpreter.calls.failures["transport"]
    if lost + drafter.calls.failures["transport"]:
        message = f"{case.case_id}: a call failed in transport"
        raise CaptureError(message)
    return tuple(client.recordings)


async def capture(args: argparse.Namespace) -> int:
    """Capture all cases, write the bundle and replay it once."""
    notes = SOURCE / "notes.jsonl"
    data = notes.read_text()
    cases = tuple(
        EvalCase.model_validate_json(line) for line in data.splitlines()
    )
    config = load_routing_config(Path("config/llm.toml"))
    routes = {
        purpose: chain[0].model_copy(
            update={"model": args.model, "effort": args.effort}
        )
        for purpose, chain in config.routes.items()
    }
    client = build_openai_client(load_openai_settings())
    gate = asyncio.Semaphore(args.concurrency)

    async def bounded(case: EvalCase) -> tuple[Recording, ...]:
        async with gate:
            return await capture_case(
                case,
                CaptureClient(client, routes, case.case_id, args.attestation),
            )

    captured = await asyncio.gather(*(bounded(case) for case in cases))
    recordings = "".join(
        row.model_dump_json() + "\n" for rows in captured for row in rows
    )
    source = Manifest.model_validate_json(
        (SOURCE / "manifest.json").read_text()
    )
    manifest = Manifest(
        dataset_sha256=digest_text(data),
        recordings_sha256=digest_text(recordings),
        dataset_version=args.out.name,
        provenance=(
            f"Live {args.model} responses at {args.effort} reasoning effort, "
            f"captured from the OpenAI Responses API. {args.attestation}"
        ),
        interpretation_prompt=INTERPRET_PROMPT,
        drafting_prompt=DRAFT_PROMPT,
        pipeline_commit=source.pipeline_commit,
        pipeline_sha256=harness.pipeline_digest(),
    )
    (args.out / "recordings").mkdir(parents=True, exist_ok=True)
    (args.out / "notes.jsonl").write_text(data)
    (args.out / "recordings" / "responses.jsonl").write_text(recordings)
    (args.out / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n"
    )
    rows = tuple(row for found in captured for row in found)
    result = await harness.evaluate(
        cases, rows, manifest, require_provider=True
    )
    print(
        json.dumps(
            {
                "recordings": len(rows),
                "provider_quality": result.provider_quality,
                "macro_f1": result.scores.macro_f1,
                "gate_failures": result.gate_failures,
            }
        )
    )
    return 0


def main() -> int:
    """Parse arguments and run the capture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--attestation", required=True)
    parser.add_argument("--concurrency", type=int, default=6)
    try:
        return asyncio.run(capture(parser.parse_args()))
    except CaptureError as error:
        print(
            json.dumps({"error": "capture_incomplete", "detail": str(error)})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
