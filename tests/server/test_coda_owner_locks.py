from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from omnigent.server.coda_owner_locks import (
    BlockingCallCompletedAfterCancellation,
    CodaOwnerLockRegistry,
    get_coda_owner_locks,
    run_awaitable_cancellation_safe,
    run_blocking_cancellation_safe,
)


async def test_different_owners_allocate_concurrently_and_registry_reclaims() -> None:
    registry = CodaOwnerLockRegistry()
    alice_entered = asyncio.Event()
    bob_entered = asyncio.Event()
    release = asyncio.Event()
    inside: set[str] = set()

    async def allocate_workspace(owner: str, entered: asyncio.Event) -> None:
        async with registry.hold(owner):
            inside.add(owner)
            entered.set()
            await release.wait()
            inside.remove(owner)

    alice = asyncio.create_task(allocate_workspace("alice", alice_entered))
    await alice_entered.wait()
    bob = asyncio.create_task(allocate_workspace("bob", bob_entered))
    await bob_entered.wait()

    assert inside == {"alice", "bob"}
    assert registry.owner_count == 2

    release.set()
    await asyncio.gather(alice, bob)
    assert registry.owner_count == 0


async def test_same_owner_allocation_is_serialized_and_entry_is_reused() -> None:
    registry = CodaOwnerLockRegistry()
    first_entered = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    active = 0
    max_active = 0

    async def first_allocation() -> None:
        nonlocal active, max_active
        async with registry.hold("alice"):
            active += 1
            max_active = max(max_active, active)
            first_entered.set()
            await release_first.wait()
            active -= 1

    async def second_allocation() -> None:
        nonlocal active, max_active
        second_attempted.set()
        async with registry.hold("alice"):
            active += 1
            max_active = max(max_active, active)
            second_entered.set()
            await release_second.wait()
            active -= 1

    first = asyncio.create_task(first_allocation())
    await first_entered.wait()
    second = asyncio.create_task(second_allocation())
    await second_attempted.wait()

    assert not second_entered.is_set()
    assert registry.owner_count == 1

    release_first.set()
    await second_entered.wait()
    assert max_active == 1
    assert registry.owner_count == 1

    release_second.set()
    await asyncio.gather(first, second)
    assert registry.owner_count == 0


async def test_cancelled_waiter_does_not_remove_live_owner_entry() -> None:
    registry = CodaOwnerLockRegistry()
    holder_entered = asyncio.Event()
    waiter_attempted = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder() -> None:
        async with registry.hold("alice"):
            holder_entered.set()
            await release_holder.wait()

    async def waiter() -> None:
        waiter_attempted.set()
        async with registry.hold("alice"):
            raise AssertionError("cancelled waiter entered the owner lock")

    holder_task = asyncio.create_task(holder())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await waiter_attempted.wait()
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    assert registry.owner_count == 1
    release_holder.set()
    await holder_task
    assert registry.owner_count == 0


async def test_failed_allocation_releases_and_reclaims_owner_lock() -> None:
    registry = CodaOwnerLockRegistry()

    with pytest.raises(RuntimeError, match="allocation failed"):
        async with registry.hold("alice"):
            raise RuntimeError("allocation failed")

    assert registry.owner_count == 0
    async with registry.hold("alice"):
        assert registry.owner_count == 1
    assert registry.owner_count == 0


async def test_timed_out_waiter_keeps_holder_entry_then_reclaims() -> None:
    registry = CodaOwnerLockRegistry()
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder() -> None:
        async with registry.hold("alice"):
            holder_entered.set()
            await release_holder.wait()

    holder_task = asyncio.create_task(holder())
    await holder_entered.wait()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            async with registry.hold("alice"):
                raise AssertionError("timed-out waiter entered the owner lock")

    assert registry.owner_count == 1
    release_holder.set()
    await holder_task
    assert registry.owner_count == 0


async def test_close_rejects_new_holders_without_discarding_active_entry() -> None:
    registry = CodaOwnerLockRegistry()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def holder() -> None:
        async with registry.hold("alice"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    await registry.close()
    assert registry.owner_count == 1
    with pytest.raises(RuntimeError, match="registry is closed"):
        async with registry.hold("bob"):
            pass

    release.set()
    await task
    assert registry.owner_count == 0


async def test_cancelled_blocking_call_finishes_before_reporting_cancellation() -> None:
    started = threading.Event()
    finish = threading.Event()

    def allocate() -> str:
        started.set()
        assert finish.wait(timeout=5)
        return "allocated-workspace"

    task = asyncio.create_task(run_blocking_cancellation_safe(allocate))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    assert not task.done()
    finish.set()
    with pytest.raises(BlockingCallCompletedAfterCancellation) as exc_info:
        await task
    assert exc_info.value.result == "allocated-workspace"


async def test_cancelled_async_side_effect_finishes_before_reporting_cancellation() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def terminate() -> str:
        started.set()
        await finish.wait()
        return "terminated"

    task = asyncio.create_task(run_awaitable_cancellation_safe(terminate()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(BlockingCallCompletedAfterCancellation) as exc_info:
        await task
    assert exc_info.value.result == "terminated"


async def test_closed_registry_runs_create_rollback_callback() -> None:
    registry = CodaOwnerLockRegistry()
    rolled_back = asyncio.Event()
    await registry.close()

    async def rollback() -> None:
        rolled_back.set()

    with pytest.raises(RuntimeError, match="registry is closed"):
        async with registry.hold("alice", on_closed=rollback):
            raise AssertionError("closed registry admitted a holder")
    assert rolled_back.is_set()
    assert registry.owner_count == 0


async def test_release_winning_owner_lock_prevents_stale_relaunch_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    registry = CodaOwnerLockRegistry()
    app_state = SimpleNamespace(coda_owner_locks=registry, agent_store=None)
    current = SimpleNamespace(host_id="host-old")
    store = SimpleNamespace(get_conversation=lambda _session_id: current)
    tracker = SimpleNamespace(
        begin=lambda _session_id: None,
        finish_calls=[],
        finish=lambda session_id: tracker.finish_calls.append(session_id),
    )
    launch_calls: list[str] = []

    async def fake_launch(**kwargs: object) -> None:
        launch_calls.append(str(kwargs["session_id"]))

    monkeypatch.setattr(orchestration, "_run_managed_launch", fake_launch)
    conv = SimpleNamespace(labels={}, agent_id=None, host_id="host-old")
    host = SimpleNamespace(user_id="alice", host_id="host-old")

    async with registry.hold("alice"):
        orchestration._kick_managed_relaunch(
            session_id="conv-release-race",
            conv=conv,
            host=host,
            sandbox_config=SimpleNamespace(),
            tracker=tracker,
            conversation_store=store,
            host_store=SimpleNamespace(),
            app_state=app_state,
        )
        await asyncio.sleep(0)
        current.host_id = None

    outstanding = list(orchestration._managed_launch_tasks)
    if outstanding:
        await asyncio.gather(*outstanding)
    assert launch_calls == []
    assert tracker.finish_calls == ["conv-release-race"]
    assert registry.owner_count == 0


async def test_closed_registry_settles_background_relaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    registry = CodaOwnerLockRegistry()
    await registry.close()
    failed: list[tuple[str, str]] = []
    statuses: list[tuple[str, str, str]] = []
    tracker = SimpleNamespace(
        begin=lambda _session_id: None,
        fail=lambda session_id, reason: failed.append((session_id, reason)),
    )
    monkeypatch.setattr(
        orchestration,
        "_publish_sandbox_status",
        lambda session_id, status, reason=None: statuses.append(
            (session_id, status, str(reason or ""))
        ),
    )

    orchestration._kick_managed_relaunch(
        session_id="conv-closed-registry",
        conv=SimpleNamespace(labels={}, agent_id=None, host_id="host-old"),
        host=SimpleNamespace(user_id="alice", host_id="host-old"),
        sandbox_config=SimpleNamespace(),
        tracker=tracker,
        conversation_store=SimpleNamespace(),
        host_store=SimpleNamespace(),
        app_state=SimpleNamespace(coda_owner_locks=registry, agent_store=None),
    )
    outstanding = list(orchestration._managed_launch_tasks)
    if outstanding:
        await asyncio.gather(*outstanding)

    reason = "owner coordination is unavailable"
    assert failed == [("conv-closed-registry", reason)]
    assert statuses[-1] == ("conv-closed-registry", "failed", reason)


def test_app_state_reuses_one_registry() -> None:
    state = SimpleNamespace()
    first = get_coda_owner_locks(state)
    second = get_coda_owner_locks(state)

    assert first is second
    assert first.owner_count == 0
