"""Durable first-write-wins coverage for authenticated native callbacks."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import mysql

from omnigent.db.db_models import workspace_scope
from omnigent.entities import ConversationItem, MessageData, NewConversationItem
from omnigent.stores.conversation_store import CallbackPendingInputEffects
from omnigent.stores.conversation_store.sqlalchemy_store import (
    CallbackIdempotencyConflictError,
    SqlAlchemyConversationStore,
    _callback_idempotency_delete_statement,
)


def _item(text_value: str = "hello") -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp_native",
        data=MessageData(
            role="assistant",
            agent="pi",
            content=[{"type": "output_text", "text": text_value}],
        ),
    )


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _append(
    store: SqlAlchemyConversationStore,
    conversation_id: str,
    key: str,
    *,
    actor: str = "alice@example.com",
    payload: str = "payload-a",
    retention_seconds: int = 604800,
) -> tuple[ConversationItem, bool, CallbackPendingInputEffects]:
    return store.append_idempotent_callback(
        conversation_id,
        lambda: (_item(payload), CallbackPendingInputEffects()),
        event_type="external_conversation_item",
        idempotency_key=key,
        actor_scope=actor,
        payload_digest=_digest(payload),
        retention_seconds=retention_seconds,
    )


def test_concurrent_response_loss_retry_and_restart_have_one_effect(db_uri: str) -> None:
    """Concurrent ambiguous retries and a fresh store return one committed item."""
    seed = SqlAlchemyConversationStore(db_uri)
    conversation_id = seed.create_conversation().id
    barrier = threading.Barrier(2)

    def attempt() -> tuple[ConversationItem, bool]:
        store = SqlAlchemyConversationStore(db_uri)
        barrier.wait(timeout=10)
        return _append(store, conversation_id, "native:logical-operation")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: attempt(), range(2)))

    item_ids = {result[0].id for result in results}
    assert len(item_ids) == 1
    assert sorted(result[1] for result in results) == [False, True]
    assert len(seed.list_items(conversation_id).data) == 1

    # A process restart is represented by a new store with no in-memory state.
    replay = _append(
        SqlAlchemyConversationStore(db_uri),
        conversation_id,
        "native:logical-operation",
    )
    created_result = next(result for result in results if result[1])
    assert replay[0] == created_result[0]
    assert replay[1] is False
    assert len(seed.list_items(conversation_id).data) == 1


def test_changed_payload_or_scope_is_rejected_without_binding_details(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    first = store.create_conversation().id
    second = store.create_conversation().id
    _append(store, first, "native:bound-key")

    attempts = (
        (first, "alice@example.com", "payload-b"),
        (first, "bob@example.com", "payload-a"),
        (second, "alice@example.com", "payload-a"),
    )
    for conversation_id, actor, payload in attempts:
        with pytest.raises(
            CallbackIdempotencyConflictError,
            match="already bound to a different callback",
        ):
            _append(
                store,
                conversation_id,
                "native:bound-key",
                actor=actor,
                payload=payload,
            )

    assert len(store.list_items(first).data) == 1
    assert store.list_items(second).data == []


def test_distinct_keys_preserve_order_and_legacy_requests_still_append(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation_id = store.create_conversation().id

    for index in range(3):
        result = _append(
            store,
            conversation_id,
            f"native:ordered-{index}",
            payload=f"payload-{index}",
        )
        assert result[1] is True

    ordered = store.list_items(conversation_id).data
    assert [item.data.content[0]["text"] for item in ordered] == [
        "payload-0",
        "payload-1",
        "payload-2",
    ]

    # No key follows the pre-contract append-on-every-request behavior.
    store.append(conversation_id, [_item("legacy")])
    store.append(conversation_id, [_item("legacy")])
    assert [item.data.content[0]["text"] for item in store.list_items(conversation_id).data].count(
        "legacy"
    ) == 2


def test_expired_records_are_reclaimed_with_bounded_cleanup(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.stores.conversation_store.sqlalchemy_store as store_module

    store = SqlAlchemyConversationStore(db_uri)
    monkeypatch.setattr(store_module, "now_epoch", lambda: 100)
    conversation_id = store.create_conversation().id
    first = _append(store, conversation_id, "native:expiring", retention_seconds=50)

    monkeypatch.setattr(store_module, "now_epoch", lambda: 1000)
    # Lifecycle maintenance reclaims idle records without requiring another
    # callback write, and each invocation has a hard work bound.
    assert store.purge_expired_callback_idempotency(retention_seconds=50, limit=1) == 1
    with store._conv_session("count_callback_idempotency_after_idle_purge") as session:
        assert session.execute(text("SELECT count(*) FROM callback_idempotency")).scalar_one() == 0

    after_expiry = _append(store, conversation_id, "native:expiring", retention_seconds=50)
    assert first[0].id != after_expiry[0].id
    assert after_expiry[1] is True
    assert len(store.list_items(conversation_id).data) == 2


@pytest.mark.asyncio
async def test_delete_conversation_removes_idempotency_rows(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation_id = store.create_conversation().id
    _append(store, conversation_id, "native:delete-cleanup")

    assert await store.delete_conversation(conversation_id) is True
    with store._conv_session("count_callback_idempotency_after_delete") as session:
        assert session.execute(text("SELECT count(*) FROM callback_idempotency")).scalar_one() == 0


def test_mysql_cleanup_delete_uses_literal_keys_without_target_subquery() -> None:
    statement = _callback_idempotency_delete_statement([(1, b"a" * 32), (2, b"b" * 32)])
    compiled = str(statement.compile(dialect=mysql.dialect())).upper()

    assert compiled.startswith("DELETE FROM CALLBACK_IDEMPOTENCY")
    assert "SELECT" not in compiled
    assert "LIMIT" not in compiled


def test_global_cleanup_orders_bounded_expiry_across_workspaces(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.stores.conversation_store.sqlalchemy_store as store_module

    store = SqlAlchemyConversationStore(db_uri)
    monkeypatch.setattr(store_module, "now_epoch", lambda: 100)
    with workspace_scope(11):
        first = store.create_conversation().id
        _append(store, first, "native:workspace-11", retention_seconds=50)
    monkeypatch.setattr(store_module, "now_epoch", lambda: 200)
    with workspace_scope(22):
        second = store.create_conversation().id
        _append(store, second, "native:workspace-22", retention_seconds=50)

    monkeypatch.setattr(store_module, "now_epoch", lambda: 1_000)
    assert store.purge_expired_callback_idempotency(retention_seconds=50, limit=1) == 1
    with store._conv_session("count_global_callback_cleanup") as session:
        remaining = (
            session.execute(text("SELECT workspace_id FROM callback_idempotency")).scalars().all()
        )
    assert remaining == [22]


def test_cleanup_batch_is_bounded(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.stores.conversation_store.sqlalchemy_store as store_module

    store = SqlAlchemyConversationStore(db_uri)
    monkeypatch.setattr(store_module, "now_epoch", lambda: 100)
    conversation_id = store.create_conversation().id
    for index in range(3):
        _append(store, conversation_id, f"native:old-{index}", retention_seconds=50)

    monkeypatch.setattr(store_module, "now_epoch", lambda: 1000)
    assert store.purge_expired_callback_idempotency(retention_seconds=50, limit=2) == 2
    with store._conv_session("count_callback_idempotency_bounded_batch") as session:
        assert session.execute(text("SELECT count(*) FROM callback_idempotency")).scalar_one() == 1
    assert store.purge_expired_callback_idempotency(retention_seconds=50, limit=2) == 1
