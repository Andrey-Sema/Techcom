"""Property-based API tests of the mock receiver (M8, Schemathesis).

Schemathesis generates requests from the published OpenAPI document and
checks every response against it: no server errors, only documented
status codes, bodies and headers that match their schemas. The same
harness runs against the product API once it exists (M6b).
"""

import json
from typing import Any
from uuid import UUID, uuid4

import schemathesis
from hypothesis import HealthCheck, settings

from support import ROOT
from techcom_mocks.app import create_app
from techcom_mocks.faults import FaultPlan
from techcom_mocks.store import DeliveryStore

TASKS = json.loads(
    (ROOT / "fixtures" / "tasks.json").read_text(encoding="utf-8")
)
APP = create_app(tasks=TASKS, store=DeliveryStore(), faults=FaultPlan())
schema = schemathesis.openapi.from_asgi("/openapi.json", APP)


@schema.parametrize()
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_mock_api_conforms_to_its_openapi(
    case: schemathesis.Case[Any],
) -> None:
    key = case.headers.get("Idempotency-Key") if case.headers else None
    if isinstance(key, str) and is_uuid(key):
        # Generated keys repeat across examples, and reusing a key with
        # another payload is rejected by design (422). Replay semantics
        # are covered by tests/unit/mocks/test_receiver.py.
        case.headers["Idempotency-Key"] = str(uuid4())
    case.call_and_validate()


def is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
