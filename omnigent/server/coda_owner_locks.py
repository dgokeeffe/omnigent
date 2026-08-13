"""Owner-scoped coordination for CoDA claim and workspace mutations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")


class BlockingCallCompletedAfterCancellation(asyncio.CancelledError):
    """Carry a blocking call's result so its side effect can be rolled back."""

    def __init__(self, result: Any) -> None:
        super().__init__()
        self.result = result


async def complete_cancellation_cleanup(awaitable: Awaitable[_T]) -> _T:
    """Finish cleanup even when an enclosing cancellation scope fires again."""
    task = asyncio.ensure_future(awaitable)
    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if current is not None:
                current.uncancel()
    return task.result()


async def run_awaitable_cancellation_safe(awaitable: Awaitable[_T]) -> _T:
    """Wait for a cancelled side effect before its caller can release a lock."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            result = await complete_cancellation_cleanup(task)
        except Exception as exc:
            raise cancelled from exc
        raise BlockingCallCompletedAfterCancellation(result) from cancelled


async def run_blocking_cancellation_safe(
    func: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Wait for a cancelled thread call so it cannot mutate state after unlock."""
    return await run_awaitable_cancellation_safe(asyncio.to_thread(func, *args, **kwargs))


@dataclass
class _OwnerLockEntry:
    lock: asyncio.Lock
    users: int = 0


class CodaOwnerLockRegistry:
    """Provide one lock per owner and reclaim entries after their last user."""

    def __init__(self) -> None:
        self._entries: dict[str, _OwnerLockEntry] = {}
        self._registry_lock = asyncio.Lock()
        self._closed = False

    @asynccontextmanager
    async def hold(
        self,
        owner: str,
        *,
        on_closed: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        """Serialize one owner's claim decisions without blocking other owners."""
        entry: _OwnerLockEntry | None = None
        async with self._registry_lock:
            if not self._closed:
                entry = self._entries.get(owner)
                if entry is None:
                    entry = _OwnerLockEntry(lock=asyncio.Lock())
                    self._entries[owner] = entry
                entry.users += 1
        if entry is None:
            if on_closed is not None:
                await on_closed()
            raise RuntimeError("CoDA owner lock registry is closed")
        try:
            async with entry.lock:
                yield
        finally:
            async with self._registry_lock:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(owner) is entry:
                    self._entries.pop(owner)

    async def close(self) -> None:
        """Prevent new acquisitions while in-flight holders drain on shutdown."""
        async with self._registry_lock:
            self._closed = True

    @property
    def owner_count(self) -> int:
        """Return retained owner entries for lifecycle checks and tests."""
        return len(self._entries)


def get_coda_owner_locks(app_state: Any) -> CodaOwnerLockRegistry:
    """Return the app's lazily initialized owner-lock registry."""
    registry = getattr(app_state, "coda_owner_locks", None)
    if registry is None:
        registry = CodaOwnerLockRegistry()
        app_state.coda_owner_locks = registry
    return registry
