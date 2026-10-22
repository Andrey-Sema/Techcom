"""Stable identities for dedupe across runs (``docs/05`` section 1).

* ``state_fingerprint`` changes when anything decision-relevant
  changes, and only then.
* ``idempotency_key`` names one logical action; it is ``UNIQUE`` in
  the outbox, so a rerun over unchanged data creates nothing new.
* ``operation_id`` is derived from the key (UUIDv5) and sent as the
  ``Idempotency-Key`` header, so it survives even a database restore.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from techcom_followup.domain.models import Decision, Task
from techcom_followup.domain.text import fold

# uuid5(NAMESPACE_URL, "urn:techcom:followup:operation"); a literal so
# that it can never change by accident.
NAMESPACE_FOLLOWUP = uuid.UUID("6ab218da-b685-5b65-9d29-7a486facc8b5")

type _JsonValue = str | int | bool | Mapping[str, _JsonValue] | None


def canonical_json(value: Mapping[str, _JsonValue]) -> str:
    """Serialise ``value`` deterministically (sorted, compact)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _sha256(value: Mapping[str, _JsonValue]) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8"))
    return digest.hexdigest()


def state_fingerprint(task: Task) -> str:
    """Hash the decision-relevant state of ``task``.

    ``last_update`` is excluded on purpose: touching a task without
    changing it must not re-notify anybody. Case and spacing noise in
    the owner and the notes do not count as a change either.
    """
    return _sha256(
        {
            "status": task.status.value,
            "due_date": (
                None if task.due_date is None else task.due_date.isoformat()
            ),
            "owner": None if task.owner is None else fold(task.owner),
            "notes": fold(task.notes.value),
        }
    )


@dataclass(frozen=True, slots=True)
class ActionIdentity:
    """The dedupe key of one outbound action and its operation id."""

    idempotency_key: str
    operation_id: uuid.UUID

    @classmethod
    def of(cls, decision: Decision, fingerprint: str) -> Self:
        """Derive the identity of the action ``decision`` asks for.

        Raises:
            ValueError: If the decision has no recipient (no action).
        """
        target = decision.target
        if target is None:
            msg = f"{decision.kind} decisions have no outbound action"
            raise ValueError(msg)
        key = "sha256:" + _sha256(
            {
                "task_id": decision.task_id,
                "decision": decision.kind.value,
                "target": {
                    "kind": target.kind.value,
                    "name": fold(target.name),
                    "project": (
                        None
                        if target.project is None
                        else fold(target.project)
                    ),
                },
                "state_fingerprint": fingerprint,
            }
        )
        return cls(key, uuid.uuid5(NAMESPACE_FOLLOWUP, key))
