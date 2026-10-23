"""A total planning deadline preserves external cancellation."""

import asyncio
from datetime import date
from typing import override

import pytest

from support import AS_OF, make_task, repo_policy
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.llm_agent_stub import (
    OfflineLanguageModel,
)
from techcom_followup.adapters.outbound.llm_stub import ScriptedLLM
from techcom_followup.application.orchestrator import FollowUpOrchestrator
from techcom_followup.application.ports import LLMRequest, LLMResult
from techcom_followup.application.repository import (
    ActionReadQuery,
    ActionRecord,
)
from techcom_followup.domain.models import Flag


class _SlowHistory(InMemoryRepository):
    @override
    async def query_actions(
        self,
        query: ActionReadQuery,
    ) -> tuple[ActionRecord, ...]:
        await asyncio.Event().wait()
        return ()


class _SlowSteps(ScriptedLLM):
    @override
    async def generate_json(self, request: LLMRequest) -> LLMResult:
        await asyncio.sleep(0.035)
        return await super().generate_json(request)


class _WaitingModel(OfflineLanguageModel):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    @override
    async def generate_json(self, request: LLMRequest) -> LLMResult:
        self.started.set()
        await asyncio.Event().wait()
        return await super().generate_json(request)


async def test_history_lookup_obeys_the_plan_deadline() -> None:
    policy = repo_policy()
    task = make_task(due_date=date(2026, 10, 18))
    model = OfflineLanguageModel()
    result = await asyncio.wait_for(
        FollowUpOrchestrator(
            model, _SlowHistory(), policy, timeout_s=0.01
        ).plan(policy.decide(task, as_of=AS_OF), task, as_of=AS_OF),
        timeout=1,
    )
    assert result.audit["outcome"] == "plan_timeout"
    assert Flag.LLM_FALLBACK in result.decision.flags
    assert result.message.source.value == "template"
    assert model.calls == 0


async def test_steps_share_one_total_deadline() -> None:
    policy = repo_policy()
    task = make_task(due_date=date(2026, 10, 18))
    model = _SlowSteps(['{"tool":"get_history","plan":null}'] * 4)
    result = await FollowUpOrchestrator(
        model,
        InMemoryRepository(),
        policy,
        timeout_s=0.05,
    ).plan(policy.decide(task, as_of=AS_OF), task, as_of=AS_OF)
    assert result.audit["outcome"] == "plan_timeout"
    assert Flag.LLM_FALLBACK in result.decision.flags
    assert len(model.requests) < 4


async def test_external_cancellation_is_not_a_successful_fallback() -> None:
    policy = repo_policy()
    task = make_task(due_date=date(2026, 10, 18))
    model = _WaitingModel()
    running = asyncio.create_task(
        FollowUpOrchestrator(
            model,
            InMemoryRepository(),
            policy,
            timeout_s=1,
        ).plan(policy.decide(task, as_of=AS_OF), task, as_of=AS_OF)
    )
    await asyncio.wait_for(model.started.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


async def test_quiet_task_is_rejected_before_loading_history() -> None:
    policy = repo_policy()
    task = make_task()
    with pytest.raises(ValueError, match="only actionable"):
        await asyncio.wait_for(
            FollowUpOrchestrator(
                OfflineLanguageModel(),
                _SlowHistory(),
                policy,
                timeout_s=0.01,
            ).plan(policy.decide(task, as_of=AS_OF), task, as_of=AS_OF),
            timeout=1,
        )
