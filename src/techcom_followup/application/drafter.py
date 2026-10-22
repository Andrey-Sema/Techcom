"""Draft placeholder messages with deterministic fallback."""

import json
from datetime import date

from pydantic import Field

from techcom_followup.application.llm_workflow import (
    InvalidOutputError,
    LanguageCall,
    StrictOutput,
    parse_output,
    require_output,
)
from techcom_followup.application.ports import (
    LLMClient,
    LLMPurpose,
    LLMRequest,
)
from techcom_followup.domain.injection import screen
from techcom_followup.domain.messages import (
    BODY_MAX_CHARS,
    BODY_MIN_CHARS,
    LLM_PLACEHOLDERS,
    SUBJECT_MAX_CHARS,
    TEMPLATES,
    DraftSource,
    MessageDraft,
    MessageValidator,
    compose,
    facts_for,
    render,
)
from techcom_followup.domain.models import Decision, Flag, Task

PROMPT_VERSION = "draft.v1"
SUBJECT_MIN_CHARS = 5
_INSTRUCTIONS = (
    "Phrase a concise construction follow-up. Output only schema-conforming "
    "JSON with subject and body. Use placeholders for every fact; do not "
    "invent names, dates, links, numbers or status changes. Preserve all "
    "placeholders in the template. The decision and recipient are final. "
    "No tools or actions are available."
)


class DraftOutput(StrictOutput):
    """A closed language-only output, before facts are inserted."""

    subject: str = Field(min_length=5, max_length=SUBJECT_MAX_CHARS)
    body: str = Field(min_length=BODY_MIN_CHARS, max_length=BODY_MAX_CHARS)


class MessageDrafter:
    """Render validated language using code-owned facts."""

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        validator: MessageValidator | None = None,
    ) -> None:
        """Inject a language port and optional name validator."""
        self.calls = LanguageCall(client)
        self._validator = validator

    async def draft(
        self, decision: Decision, task: Task, *, as_of: date
    ) -> MessageDraft:
        """Draft a decision without exposing notes or personal facts."""
        baseline = compose(decision, task, as_of)
        if (
            Flag.PROMPT_INJECTION_SUSPECTED in decision.flags
            or screen(task.notes).suspicious
        ):
            return baseline
        from_template = self._request(decision, baseline.template_id)
        text = await self.calls.call(from_template)
        if text is None:
            return baseline
        try:
            output = parse_output(text, DraftOutput)
            validator = self._validator or MessageValidator([task.owner or ""])
            require_output(
                not validator.violations(output.subject, output.body),
                "message_rules",
            )
            require_output(
                all(
                    "{" + name + "}" in output.subject + output.body
                    for name in _required(baseline.template_id)
                ),
                "missing_placeholder",
            )
            facts = facts_for(decision, task, as_of)
            body = render(output.body, facts)
            if "{owner_name}" in TEMPLATES[baseline.template_id].body:
                body += (
                    f" Owner: {facts['owner_name']}. "
                    f"Context: {facts['reason_text']}"
                )
            result = MessageDraft(
                subject=render(output.subject, facts),
                body=body,
                template_id=baseline.template_id,
                source=DraftSource.LLM,
            )
            require_output(
                SUBJECT_MIN_CHARS <= len(result.subject) <= SUBJECT_MAX_CHARS
                and BODY_MIN_CHARS <= len(result.body) <= BODY_MAX_CHARS,
                "rendered_length",
            )
        except InvalidOutputError as exc:
            self.calls.reject(str(exc), from_template, text)
            return baseline
        self.calls.accept()
        return result

    @staticmethod
    def _request(decision: Decision, template_id: str) -> LLMRequest:
        return LLMRequest(
            purpose=LLMPurpose.DRAFT_MESSAGE,
            instructions=_INSTRUCTIONS,
            input_text=json.dumps(
                {
                    "decision": decision.kind,
                    "reason_code": decision.reason_code,
                    "template_id": template_id,
                    "allowed_placeholders": sorted(LLM_PLACEHOLDERS),
                    "required_placeholders": _required(template_id),
                }
            ),
            schema_name="draft_body",
            json_schema=DraftOutput.model_json_schema(),
            prompt_version=PROMPT_VERSION,
        )


def _required(template_id: str) -> list[str]:
    template = TEMPLATES[template_id]
    text = template.subject + template.body
    return sorted(
        name for name in LLM_PLACEHOLDERS if "{" + name + "}" in text
    )
