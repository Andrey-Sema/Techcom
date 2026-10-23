"""Select approved phrasing and render facts owned entirely by code.

Free-form language cannot be proven factual by name/date regexes. The
model therefore selects an exact entry from a small phrasing catalogue;
unknown prose always falls back. This removes the ability to invent
names, instructions or facts even when the injection screen misses them.
"""

import json
from datetime import date

from pydantic import Field

from techcom_followup.application.llm_workflow import (
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
from techcom_followup.domain.injection import screen_task
from techcom_followup.domain.messages import (
    BODY_MAX_CHARS,
    BODY_MIN_CHARS,
    LLM_PLACEHOLDERS,
    SUBJECT_MAX_CHARS,
    TEMPLATES,
    DraftSource,
    MessageDraft,
    MessageTemplate,
    MessageValidator,
    compose,
    facts_for,
    render_draft,
)
from techcom_followup.domain.models import Decision, Flag, Task

PROMPT_VERSION = "draft.v2"
_INSTRUCTIONS = (
    "Select one approved phrasing exactly. Output only schema-conforming "
    "JSON with its subject and body, unchanged. Facts are placeholders "
    "filled by code. Do not add names, instructions or facts. The decision "
    "and recipient are final. No tools or actions are available."
)


class DraftOutput(StrictOutput):
    """An exact catalogue entry, before code inserts any facts."""

    subject: str = Field(min_length=5, max_length=SUBJECT_MAX_CHARS)
    body: str = Field(min_length=BODY_MIN_CHARS, max_length=BODY_MAX_CHARS)


def approved_phrasings(template_id: str) -> tuple[DraftOutput, ...]:
    """Offer code-authored variants with identical facts."""
    template = TEMPLATES[template_id]
    body = template.body
    if "{owner_name}" in body:
        body = (
            "Hello {recipient_name}, {task_id} '{task_title}' ({project}) "
            "needs attention. Due: {due_date}."
        )
    return (
        DraftOutput(subject=template.subject, body=body),
        DraftOutput(subject=template.subject, body=body + " Thank you."),
    )


class MessageDrafter:
    """Select approved phrasing with code-owned factual values."""

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
            or screen_task(task).suspicious
        ):
            return baseline
        choices = approved_phrasings(baseline.template_id)
        output = await self.calls.call(
            self._request(decision, choices),
            lambda text: self._validate(text, choices, task),
        )
        if output is None:
            return baseline
        facts = facts_for(decision, task, as_of)
        body = output.body
        if "{owner_name}" in TEMPLATES[baseline.template_id].body:
            body += " Owner: {owner_name}. Context: {reason_text}"
        try:
            return render_draft(
                MessageTemplate(baseline.template_id, output.subject, body),
                facts,
                source=DraftSource.LLM,
            )
        except ValueError:
            return baseline

    def _validate(
        self, text: str, choices: tuple[DraftOutput, ...], task: Task
    ) -> DraftOutput:
        output = parse_output(text, DraftOutput)
        require_output(output in choices, "unapproved_phrasing")
        validator = self._validator or MessageValidator([task.owner or ""])
        require_output(
            not validator.violations(output.subject, output.body),
            "message_rules",
        )
        return output

    @staticmethod
    def _request(
        decision: Decision, choices: tuple[DraftOutput, ...]
    ) -> LLMRequest:
        return LLMRequest(
            purpose=LLMPurpose.DRAFT_MESSAGE,
            instructions=_INSTRUCTIONS,
            input_text=json.dumps(
                {
                    "decision": decision.kind,
                    "reason_code": decision.reason_code,
                    "allowed_placeholders": sorted(LLM_PLACEHOLDERS),
                    "approved_phrasings": [
                        item.model_dump() for item in choices
                    ],
                }
            ),
            schema_name="draft_body",
            json_schema=DraftOutput.model_json_schema(),
            prompt_version=PROMPT_VERSION,
        )
