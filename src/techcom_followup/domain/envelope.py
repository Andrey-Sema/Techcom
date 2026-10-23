"""The action envelope: what an LLM planner is allowed to choose.

The policy decides *that* a task needs attention and computes a
baseline action. The envelope then lists every alternative the code
considers safe for the same facts: which decision kinds, which
recipients (by opaque reference, never by free text) and the lowest
acceptable priority. A planner can choose inside it; it cannot widen
it, silence an actionable task or invent a recipient.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from pydantic import JsonValue

from techcom_followup.domain.models import (
    Decision,
    DecisionKind,
    Priority,
    Target,
    TargetKind,
    Task,
)
from techcom_followup.domain.policy import RoutingTable

ASSIGNED_TEAM = "assigned_team"
_PRIORITY_RANK: Mapping[Priority, int] = {
    Priority.NORMAL: 0,
    Priority.HIGH: 1,
}


def priority_rank(priority: Priority) -> int:
    """Order priorities so that a planner can raise, never lower."""
    return _PRIORITY_RANK[priority]


@dataclass(frozen=True, slots=True)
class ActionEnvelope:
    """Allowed (decision, recipient) pairs around a baseline."""

    baseline: Decision
    recipients: Mapping[str, Target]
    options: Mapping[DecisionKind, frozenset[str]]
    min_priority: Priority

    def permits_kind(self, kind: DecisionKind) -> bool:
        """Return whether ``kind`` is an allowed decision."""
        return kind in self.options

    def permits(self, kind: DecisionKind, recipient: str) -> bool:
        """Return whether ``recipient`` may receive ``kind``."""
        return recipient in self.options.get(kind, frozenset())

    def target(self, recipient: str) -> Target:
        """Resolve an opaque recipient reference to a code-owned target.

        Raises:
            KeyError: The reference is not part of the envelope.
        """
        return self.recipients[recipient]

    def describe(self) -> dict[str, JsonValue]:
        """Describe the choices to a model without personal names."""
        return {
            "baseline": {
                "decision": self.baseline.kind.value,
                "recipient": self._baseline_ref(),
                "priority": self.baseline.priority.value,
            },
            "recipients": {
                ref: target.kind.value
                for ref, target in sorted(self.recipients.items())
            },
            "allowed": {
                kind.value: list[JsonValue](sorted(refs))
                for kind, refs in sorted(self.options.items())
            },
            "min_priority": self.min_priority.value,
        }

    def _baseline_ref(self) -> str:
        return next(
            ref
            for ref, target in sorted(self.recipients.items())
            if target == self.baseline.target
        )


def build_envelope(
    decision: Decision,
    task: Task,
    routing: RoutingTable,
    *,
    as_of: date,
    prior_follow_ups: int,
) -> ActionEnvelope | None:
    """Return the envelope of an actionable decision, else ``None``.

    Rules (all deterministic):

    * the baseline action is always allowed;
    * clarification may also go to the project coordinator;
    * a materials ETA request may also go to the task owner;
    * an overdue follow-up or ETA request may be raised to the project
      manager once an earlier action for the task was already sent:
      a first contact always follows the policy's routing;
    * an escalation may go to the manager or the coordinator, but it
      can never be downgraded;
    * no option ever leads to ``no_action``.
    """
    if not decision.actionable or decision.target is None:
        return None
    recipients = _recipients(decision.target, task, routing)
    baseline_ref = next(
        ref for ref, target in recipients.items() if target == decision.target
    )
    options: dict[DecisionKind, frozenset[str]] = {
        decision.kind: frozenset({baseline_ref})
        | _alternatives(decision.kind, recipients)
    }
    if decision.kind in {
        DecisionKind.FOLLOW_UP_OWNER,
        DecisionKind.REQUEST_ETA,
    } and _already_chased(task, as_of=as_of, chased=prior_follow_ups):
        options[DecisionKind.ESCALATE] = frozenset({"manager"})
    return ActionEnvelope(
        baseline=decision,
        recipients=MappingProxyType(recipients),
        options=MappingProxyType(options),
        min_priority=decision.priority,
    )


def _recipients(
    baseline: Target, task: Task, routing: RoutingTable
) -> dict[str, Target]:
    recipients = {
        "coordinator": routing.coordinator(task.project),
        "manager": routing.manager(task.project),
    }
    if task.owner is not None:
        recipients["owner"] = Target(
            TargetKind.PERSON, task.owner, task.project
        )
    if baseline not in recipients.values():
        recipients[ASSIGNED_TEAM] = baseline
    return recipients


def _alternatives(
    kind: DecisionKind, recipients: Mapping[str, Target]
) -> frozenset[str]:
    if kind is DecisionKind.CLARIFY_MISSING_INFO:
        return frozenset({"coordinator"})
    if kind is DecisionKind.REQUEST_ETA and "owner" in recipients:
        return frozenset({"owner"})
    if kind is DecisionKind.ESCALATE:
        return frozenset({"manager", "coordinator"})
    return frozenset()


def _already_chased(task: Task, *, as_of: date, chased: int) -> bool:
    delta = task.days_until_due(as_of)
    return chased > 0 and delta is not None and delta < 0
