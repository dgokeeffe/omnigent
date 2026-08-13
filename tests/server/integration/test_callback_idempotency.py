"""Authenticated route contract for durable native callback idempotency."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.runtime import pending_inputs
from omnigent.server.auth import LEVEL_EDIT
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.integration.test_sessions_attribution import _seed_shared_session

pytest_plugins = ("tests.server.integration.test_sessions_attribution",)


def _callback(
    key: str | None,
    text: str = "native output",
    *,
    role: str = "assistant",
) -> dict[str, object]:
    body: dict[str, object] = {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "item_data": {
                "role": role,
                "agent": "pi",
                "content": [{"type": "output_text", "text": text}],
            },
            "response_id": "resp_native",
        },
    }
    if key is not None:
        body["idempotency_key"] = key
    return body


@pytest.mark.asyncio
async def test_lost_response_retry_returns_identical_ack_without_duplicate(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}
    body = _callback("pi:lost-response")

    # Ignore the first response to model a commit followed by transport loss.
    first = await auth_client.post(f"/v1/sessions/{session_id}/events", json=body, headers=headers)
    retry = await auth_client.post(f"/v1/sessions/{session_id}/events", json=body, headers=headers)

    assert first.status_code == retry.status_code == 202
    assert retry.json() == first.json()
    assert len(SqlAlchemyConversationStore(db_uri).list_items(session_id).data) == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_creates_exactly_one_route_effect(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}
    body = _callback("pi:concurrent")

    responses = await asyncio.gather(
        auth_client.post(f"/v1/sessions/{session_id}/events", json=body, headers=headers),
        auth_client.post(f"/v1/sessions/{session_id}/events", json=body, headers=headers),
    )

    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    assert len(SqlAlchemyConversationStore(db_uri).list_items(session_id).data) == 1


@pytest.mark.asyncio
async def test_concurrent_replay_does_not_consume_next_pending_input(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    pending_inputs.reset_for_tests()
    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}
    first_id = pending_inputs.record(
        session_id,
        [
            {"type": "input_text", "text": "first"},
            {"type": "input_image", "file_id": "file_first", "filename": "first.png"},
        ],
        "alice@example.com",
    )
    second_id = pending_inputs.record(
        session_id,
        [{"type": "input_text", "text": "second"}],
        "alice@example.com",
    )
    body = _callback("pi:pending-race", "first", role="user")

    responses = await asyncio.gather(
        auth_client.post(f"/v1/sessions/{session_id}/events", json=body, headers=headers),
        auth_client.post(f"/v1/sessions/{session_id}/events", json=body, headers=headers),
    )

    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    assert pending_inputs.snapshot_for(session_id) == [
        {
            "pending_id": second_id,
            "content": [{"type": "input_text", "text": "second"}],
            "created_by": "alice@example.com",
        }
    ]
    item = SqlAlchemyConversationStore(db_uri).list_items(session_id).data[0]
    assert first_id != second_id
    assert any(block.get("file_id") == "file_first" for block in item.data.content)
    pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_replay_recovers_pending_resolution_cancelled_after_commit(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_inputs.reset_for_tests()
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    conv = store.get_conversation(session_id)
    assert conv is not None
    first_id = pending_inputs.record(
        session_id,
        [
            {"type": "input_text", "text": "first"},
            {"type": "input_image", "file_id": "file_first"},
        ],
        "alice@example.com",
    )
    second_id = pending_inputs.record(
        session_id,
        [{"type": "input_text", "text": "second"}],
        "alice@example.com",
    )
    body = SessionEventInput.model_validate(
        _callback("pi:cancel-after-commit", "first", role="user")
    )
    real_resolve_ids = pending_inputs.resolve_ids
    calls = 0

    def cancel_once(target_session_id: str, pending_ids: list[str]):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return real_resolve_ids(target_session_id, pending_ids)

    monkeypatch.setattr(pending_inputs, "resolve_ids", cancel_once)
    with pytest.raises(asyncio.CancelledError):
        await _persist_external_conversation_item(
            session_id,
            conv,
            body,
            store,
            created_by="alice@example.com",
            callback_actor_scope="alice@example.com",
        )

    # The item and winner-selected pending id committed, but no queue side
    # effect ran. Replay must resolve that same id, never peek at the next one.
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        first_id,
        second_id,
    ]
    replayed_id = await _persist_external_conversation_item(
        session_id,
        conv,
        body,
        store,
        created_by="alice@example.com",
        callback_actor_scope="alice@example.com",
    )

    items = store.list_items(session_id).data
    assert len(items) == 1
    assert replayed_id == items[0].id
    assert any(block.get("file_id") == "file_first" for block in items[0].data.content)
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        second_id
    ]
    pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_replay_republishes_original_item_after_commit(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.server.routes._sessions.orchestration as orchestration

    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}
    body = _callback("pi:republish")
    published: list[str] = []
    monkeypatch.setattr(
        orchestration,
        "_publish_external_conversation_item",
        lambda _session_id, item, **_kwargs: published.append(item.id),
    )

    first = await auth_client.post(f"/v1/sessions/{session_id}/events", json=body, headers=headers)
    published.clear()  # Model process loss after commit but before publication.
    replay = await auth_client.post(
        f"/v1/sessions/{session_id}/events", json=body, headers=headers
    )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert published == [first.json()["item_id"]]
    assert len(SqlAlchemyConversationStore(db_uri).list_items(session_id).data) == 1


@pytest.mark.asyncio
async def test_key_reuse_conflicts_are_generic_across_payload_actor_and_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    grants = {"alice@example.com": LEVEL_EDIT, "bob@example.com": LEVEL_EDIT}
    first_session = _seed_shared_session(db_uri, grants)
    second_session = _seed_shared_session(db_uri, grants)
    key = "pi:scope-bound"
    alice = {"X-Forwarded-Email": "alice@example.com"}
    bob = {"X-Forwarded-Email": "bob@example.com"}

    accepted = await auth_client.post(
        f"/v1/sessions/{first_session}/events", json=_callback(key), headers=alice
    )
    assert accepted.status_code == 202

    conflicts = [
        await auth_client.post(
            f"/v1/sessions/{first_session}/events",
            json=_callback(key, "changed"),
            headers=alice,
        ),
        await auth_client.post(
            f"/v1/sessions/{first_session}/events", json=_callback(key), headers=bob
        ),
        await auth_client.post(
            f"/v1/sessions/{second_session}/events", json=_callback(key), headers=alice
        ),
    ]
    assert {response.status_code for response in conflicts} == {400}
    assert len({response.text for response in conflicts}) == 1
    assert key not in conflicts[0].text
    assert len(SqlAlchemyConversationStore(db_uri).list_items(first_session).data) == 1
    assert SqlAlchemyConversationStore(db_uri).list_items(second_session).data == []


@pytest.mark.asyncio
async def test_legacy_no_key_and_non_durable_key_contract(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})
    headers = {"X-Forwarded-Email": "alice@example.com"}

    legacy = _callback(None)
    first = await auth_client.post(
        f"/v1/sessions/{session_id}/events", json=legacy, headers=headers
    )
    second = await auth_client.post(
        f"/v1/sessions/{session_id}/events", json=legacy, headers=headers
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["item_id"] != second.json()["item_id"]

    unsupported = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "interrupt", "data": {}, "idempotency_key": "pi:not-durable"},
        headers=headers,
    )
    assert unsupported.status_code == 400
