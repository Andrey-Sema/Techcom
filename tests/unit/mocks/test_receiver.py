import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from techcom_mocks.app import create_app
from techcom_mocks.faults import Fault, FaultInjectingTransport, FaultPlan
from techcom_mocks.models import DeliveryPayload
from techcom_mocks.store import (
    DeliveryStore,
    StoreUnavailableError,
    payload_digest,
)


def payload(
    body: str = "Please provide the delivery date.",
) -> DeliveryPayload:
    return DeliveryPayload.model_validate(
        {
            "task_id": "TC-1002",
            "target": {"kind": "team", "name": "warehouse"},
            "message": {
                "subject": "Delivery date needed",
                "body": body,
                "template_id": "request_eta.v1",
            },
        }
    )


def client_for(
    store: DeliveryStore | None = None,
    faults: FaultPlan | None = None,
    transport_plan: FaultPlan | None = None,
) -> httpx.AsyncClient:
    app = create_app(
        tasks=[{"task_id": "TC-1002"}], store=store, faults=faults
    )
    transport: httpx.AsyncBaseTransport = httpx.ASGITransport(app=app)
    if transport_plan is not None:
        transport = FaultInjectingTransport(transport, transport_plan)
    return httpx.AsyncClient(transport=transport, base_url="http://mock")


async def send(
    client: httpx.AsyncClient,
    operation_id: UUID,
    value: DeliveryPayload | None = None,
) -> httpx.Response:
    return await client.post(
        "/v1/messages",
        headers={"Idempotency-Key": str(operation_id)},
        json=(payload() if value is None else value).model_dump(mode="json"),
    )


async def test_tasks_and_missing_lookup() -> None:
    async with client_for() as client:
        assert (await client.get("/v1/tasks")).json() == [
            {"task_id": "TC-1002"}
        ]
        assert (await client.get(f"/v1/messages/{uuid4()}")).status_code == 404


async def test_replay_conflict_and_recovery(tmp_path: Path) -> None:
    journal = tmp_path / "delivery.jsonl"
    store = DeliveryStore(journal)
    operation_id = uuid4()
    async with client_for(store) as client:
        first = await send(client, operation_id)
        replay = await send(client, operation_id)
        mismatch = await send(
            client, operation_id, payload("A different message body here.")
        )
        found = await client.get(f"/v1/messages/{operation_id}")
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json() == found.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert mismatch.status_code == 422
    assert store.count == 1
    restored = DeliveryStore(journal)
    receipt, replayed = await restored.deliver(operation_id, payload())
    assert replayed
    assert str(receipt.operation_id) == first.json()["operation_id"]
    assert restored.count == 1
    assert len(journal.read_text().splitlines()) == 1


async def test_competing_requests_commit_once() -> None:
    store = DeliveryStore()
    operation_id = uuid4()
    async with client_for(store) as client:
        responses = await asyncio.gather(
            *(send(client, operation_id) for _ in range(20))
        )
        assert sorted(r.status_code for r in responses) == [201] + [409] * 19
        assert (await send(client, operation_id)).status_code == 201
    assert store.count == 1


async def test_cancelled_reservation_can_be_retried() -> None:
    store = DeliveryStore()
    operation_id = uuid4()
    task = asyncio.create_task(store.deliver(operation_id, payload()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.lookup(operation_id) is None
    _, replayed = await store.deliver(operation_id, payload())
    assert not replayed
    assert store.count == 1


async def test_pending_lookup_never_signals_safe_to_resend() -> None:
    store = DeliveryStore()
    operation_id = uuid4()
    original = asyncio.create_task(
        store.deliver(operation_id, payload(), idempotent=False)
    )
    await asyncio.sleep(0)
    async with client_for(store) as client:
        uncertain = await client.get(f"/v1/messages/{operation_id}")
        assert uncertain.status_code == 409
        assert uncertain.headers["Retry-After"] == "1"
        await original
        found = await client.get(f"/v1/messages/{operation_id}")
        assert found.status_code == 200
    assert store.count == 1


async def test_non_idempotent_receiver_really_duplicates() -> None:
    store = DeliveryStore()
    operation_id = uuid4()
    first, _ = await store.deliver(operation_id, payload(), idempotent=False)
    second, replayed = await store.deliver(
        operation_id, payload(), idempotent=False
    )
    assert not replayed
    assert first.provider_ref != second.provider_ref
    assert store.count == 2
    assert await store.lookup(operation_id) == second


async def test_timeout_after_commit_can_reconcile_without_resending(
    tmp_path: Path,
) -> None:
    store = DeliveryStore(tmp_path / "messages.jsonl")
    operation_id = uuid4()
    plan = FaultPlan.parse("messages:timeout_after_commit@1")
    async with client_for(store, transport_plan=plan) as client:
        with pytest.raises(httpx.ReadTimeout):
            await send(client, operation_id)
        found = await client.get(f"/v1/messages/{operation_id}")
        assert found.status_code == 200
        replay = await send(client, operation_id)
        assert replay.headers["Idempotent-Replayed"] == "true"
        assert found.json() == replay.json()
    assert store.count == 1


async def test_timeout_before_commit_is_absent_and_retryable() -> None:
    store = DeliveryStore()
    operation_id = uuid4()
    async with client_for(
        store,
        transport_plan=FaultPlan.parse("messages:timeout_before_commit@1"),
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            await send(client, operation_id)
        assert (
            await client.get(f"/v1/messages/{operation_id}")
        ).status_code == 404
        assert (await send(client, operation_id)).status_code == 201
    assert store.count == 1


@pytest.mark.parametrize(
    "plan", ["tasks:429@1", "tasks:500@1", "tasks:503@1", "tasks:malformed@1"]
)
async def test_http_faults_from_receiver_and_transport(plan: str) -> None:
    for receiver_faults, transport_plan in [
        (FaultPlan.parse(plan), None),
        (None, FaultPlan.parse(plan)),
    ]:
        async with client_for(
            faults=receiver_faults, transport_plan=transport_plan
        ) as client:
            failed = await client.get("/v1/tasks")
            if "malformed" in plan:
                with pytest.raises(json.JSONDecodeError):
                    failed.json()
            else:
                assert failed.status_code == int(
                    plan.split(":")[1].split("@", maxsplit=1)[0]
                )
                assert failed.headers["Retry-After"] == "1"
            assert (await client.get("/v1/tasks")).status_code == 200


async def test_message_and_lookup_faults() -> None:
    operation_id = uuid4()
    async with client_for(
        faults=FaultPlan.parse("messages:429@1,lookup:503@1")
    ) as client:
        assert (await send(client, operation_id)).status_code == 429
        assert (
            await client.get(f"/v1/messages/{operation_id}")
        ).status_code == 503
        assert (await send(client, operation_id)).status_code == 201


async def test_transport_reset_and_non_idempotent_configuration() -> None:
    async with client_for(
        transport_plan=FaultPlan.parse("tasks:reset@1")
    ) as client:
        with pytest.raises(httpx.ReadError):
            await client.get("/v1/tasks")
    async with client_for(
        transport_plan=FaultPlan.parse("messages:non_idempotent@1")
    ) as client:
        with pytest.raises(ValueError, match="receiver"):
            await send(client, uuid4())


async def test_receiver_non_idempotent_fault_and_reset() -> None:
    store = DeliveryStore()
    operation_id = uuid4()
    async with client_for(
        store,
        faults=FaultPlan.parse(
            "messages:non_idempotent@1,messages:non_idempotent@2,lookup:reset@1"
        ),
    ) as client:
        assert (await send(client, operation_id)).status_code == 201
        assert (await send(client, operation_id)).status_code == 201
        assert (
            await client.get(f"/v1/messages/{operation_id}")
        ).status_code == 503
    assert store.count == 2


@pytest.mark.parametrize(
    "fault", ["timeout_before_commit", "timeout_after_commit"]
)
async def test_receiver_timeouts_use_injected_sleep(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    slept: list[float] = []

    async def record_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("techcom_mocks.app.asyncio.sleep", record_sleep)
    store = DeliveryStore()
    async with client_for(
        store, faults=FaultPlan.parse(f"messages:{fault}@1")
    ) as client:
        assert (await send(client, uuid4())).status_code == 504
    assert store.count == (1 if fault == "timeout_after_commit" else 0)
    assert 30 in slept


@pytest.mark.parametrize(
    "script",
    [
        "bad",
        "tasks:invalid@1",
        "else:429@1",
        "tasks:429@0",
        "tasks:429@-1",
        "tasks:429@x",
        "tasks:429@1,tasks:503@1",
    ],
)
def test_invalid_fault_configuration_fails(script: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        FaultPlan.parse(script)


def test_fault_plan_consumes_only_named_endpoint() -> None:
    plan = FaultPlan.parse(" tasks:429@2,messages:503@1 ")
    assert plan.next_fault("tasks") is None
    assert plan.next_fault("messages") is Fault.UNAVAILABLE
    assert plan.next_fault("tasks") is Fault.RATE_LIMIT


@pytest.mark.parametrize(
    ("headers", "change"),
    [
        ({}, {}),
        ({"Idempotency-Key": "invalid"}, {}),
        ({"Idempotency-Key": str(uuid4())}, {"extra": "secret-note"}),
        ({"Idempotency-Key": str(uuid4())}, {"task_id": 123}),
    ],
)
async def test_validation_does_not_echo_input(
    headers: dict[str, str], change: dict[str, object]
) -> None:
    raw = payload().model_dump(mode="json") | change
    async with client_for() as client:
        response = await client.post("/v1/messages", headers=headers, json=raw)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}


async def test_disk_failure_makes_lookup_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DeliveryStore(tmp_path / "journal.jsonl")
    operation_id = uuid4()

    def fail_fsync(_fd: int) -> None:
        msg = "injected I/O failure"
        raise OSError(msg)

    monkeypatch.setattr("techcom_mocks.store.os.fsync", fail_fsync)
    async with client_for(store) as client:
        assert (await send(client, operation_id)).status_code == 503
        assert (
            await client.get(f"/v1/messages/{operation_id}")
        ).status_code == 503
        assert (await send(client, uuid4())).status_code == 503
    assert store.count == 0


async def test_missing_journal_directory_fails_closed(tmp_path: Path) -> None:
    store = DeliveryStore(tmp_path / "missing" / "journal.jsonl")
    with pytest.raises(StoreUnavailableError):
        await store.deliver(uuid4(), payload())


def test_corrupt_journal_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    entry = {
        "payload_sha256": "wrong",
        "payload": payload().model_dump(mode="json"),
        "receipt": {"operation_id": str(uuid4()), "provider_ref": "mock-1"},
    }
    path.write_text(json.dumps(entry) + "\n")
    with pytest.raises(ValueError, match="digest"):
        DeliveryStore(path)
    path.write_text("{truncated")
    with pytest.raises(ValueError, match=r"."):
        DeliveryStore(path)


async def test_app_loads_operator_fixture_and_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "tasks.json"
    fixture.write_text('[{"task_id":"fixture"}]')
    monkeypatch.setenv("FOLLOWUP_MOCK_FIXTURE", str(fixture))
    monkeypatch.setenv(
        "FOLLOWUP_MOCK_JOURNAL", str(tmp_path / "delivery.jsonl")
    )
    monkeypatch.setenv("FOLLOWUP_MOCK_FAULTS", "tasks:429@1")
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        assert (await client.get("/v1/tasks")).status_code == 429
        assert (await client.get("/v1/tasks")).json() == [
            {"task_id": "fixture"}
        ]


# fsync latency depends on the filesystem, not payload correctness.
@given(st.text(min_size=20, max_size=800))
@settings(deadline=None)
async def test_payload_canonicalization_and_jsonl_escaping(body: str) -> None:
    value = payload(body)
    reversed_keys = dict(reversed(list(value.model_dump(mode="json").items())))
    assert payload_digest(value) == payload_digest(
        DeliveryPayload.model_validate(reversed_keys)
    )
    with TemporaryDirectory() as directory:
        journal = Path(directory) / "journal.jsonl"
        store = DeliveryStore(journal)
        operation_id = uuid4()
        await store.deliver(operation_id, value)
        assert len(journal.read_text().splitlines()) == 1
        recovered = DeliveryStore(journal)
        assert await recovered.lookup(operation_id) is not None


@given(st.uuids(), st.integers(min_value=1, max_value=30))
async def test_any_key_replays_without_additional_writes(
    operation_id: UUID, attempts: int
) -> None:
    store = DeliveryStore()
    first, replay = await store.deliver(operation_id, payload())
    assert not replay
    for _ in range(attempts):
        receipt, replay = await store.deliver(operation_id, payload())
        assert replay
        assert receipt == first
    assert store.count == 1
