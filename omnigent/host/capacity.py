"""Host runner admission and resource telemetry.

Two independent ceilings guard a host daemon:

* a hard count limit (``OMNIGENT_HOST_MAX_RUNNERS``) on *active plus
  in-flight* runner launches, and
* a cgroup-v2 memory gate that can reduce currently available capacity
  below that count limit.

Only cgroup memory accounting is consulted — v2 (``memory.current`` /
``memory.max``) first, then v1 (``memory.usage_in_bytes`` /
``memory.limit_in_bytes``), because Databricks Apps containers still expose
v1. Whole machine memory (``/proc/meminfo``, ``psutil.virtual_memory()``)
is never used: inside a container it is not the process's budget, so
treating it as one would either block a healthy laptop or claim headroom a
container does not have. When cgroup telemetry is absent the gate falls
back explicitly to the hard count limit alone.

Usage is the *working set*: reported usage minus inactive file cache (from
``memory.stat``), which is the reclaimable part. Counting reclaimable page
cache as pressure would refuse launches on a container that is merely warm,
not short of memory.

Pressure uses a latch with hysteresis so a host does not flap between
accepting and refusing:

* latch on when usage is at/above the high watermark, or when admitting
  one more runner's reserve would cross that watermark;
* latch off only when usage is at/below the resume threshold *and* the
  reserve fits under the high watermark.

The two conditions are exact complements, so a single evaluation can
never both clear and immediately re-arm the latch.

Published snapshots are advisory (a peer may read a stale one);
:meth:`HostAdmission.reserve` is the authoritative gate.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Hard ceiling on active + in-flight runners. ``0``/unset keeps the
#: documented unlimited mode for local and legacy daemons.
MAX_RUNNERS_ENV = "OMNIGENT_HOST_MAX_RUNNERS"
#: Canonical memory-policy names. They intentionally match the names CoDA
#: sets in ``app.yaml`` so a deployed value is actually consumed here.
MEMORY_HIGH_ENV = "OMNIGENT_HOST_MEMORY_HIGH_WATERMARK_PERCENT"
MEMORY_RESUME_ENV = "OMNIGENT_HOST_MEMORY_RESUME_THRESHOLD_PERCENT"
RUNNER_RESERVE_ENV = "OMNIGENT_HOST_RUNNER_MEMORY_RESERVE_MB"
CGROUP_ROOT_ENV = "OMNIGENT_CGROUP_ROOT"

DEFAULT_HIGH_PERCENT = 80.0
DEFAULT_RESUME_PERCENT = 70.0
DEFAULT_RESERVE_MB = 768

#: Wire/API reason codes. ``host_at_capacity`` is the count ceiling;
#: ``memory_pressure`` is the independent resource gate.
COUNT_LIMIT_REASON = "host_at_capacity"
MEMORY_PRESSURE_REASON = "memory_pressure"


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def configured_max_runners() -> int | None:
    """Return the fixed cap, or ``None`` for the documented unlimited mode."""
    try:
        value = int(os.environ.get(MAX_RUNNERS_ENV, "0"))
    except ValueError:
        value = 0
    return value if value > 0 else None


#: A cgroup v1 limit at/above this is the kernel's "unlimited" sentinel
#: (PAGE_COUNTER_MAX scaled to bytes), not a real budget.
V1_UNLIMITED_FLOOR = 1 << 60


def _read_int(path: Path) -> int | None:
    """Read a single integer from a cgroup file, or ``None``."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _inactive_file_bytes(stat_path: Path) -> int:
    """Reclaimable file cache from ``memory.stat``; 0 when unavailable.

    v2 spells it ``inactive_file``, v1 ``total_inactive_file``.
    """
    try:
        text = stat_path.read_text()
    except OSError:
        return 0
    for key in ("total_inactive_file", "inactive_file"):
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == key:
                try:
                    return max(0, int(fields[1]))
                except ValueError:
                    return 0
    return 0


def _read_cgroup_v2(root: Path) -> tuple[int, int] | None:
    """Read v2 ``memory.current`` / ``memory.max``.

    ``memory.max`` of ``max`` is a valid reading but carries no percentage,
    so it is reported as unavailable and the count limit applies alone.
    """
    current, maximum = root / "memory.current", root / "memory.max"
    if not (current.is_file() and maximum.is_file()):
        return None
    used = _read_int(current)
    try:
        raw_max = maximum.read_text().strip()
    except OSError:
        return None
    if used is None or raw_max == "max":
        return None
    try:
        limit = int(raw_max)
    except ValueError:
        return None
    working_set = max(0, used - _inactive_file_bytes(root / "memory.stat"))
    return (working_set, limit) if used >= 0 and limit > 0 else None


def _read_cgroup_v1(root: Path) -> tuple[int, int] | None:
    """Read v1 memory accounting from ``<root>/memory`` or ``<root>``."""
    for base in (root / "memory", root):
        usage, limit_path = base / "memory.usage_in_bytes", base / "memory.limit_in_bytes"
        if not (usage.is_file() and limit_path.is_file()):
            continue
        used, limit = _read_int(usage), _read_int(limit_path)
        if used is None or limit is None or used < 0 or limit <= 0:
            continue
        if limit >= V1_UNLIMITED_FLOOR:
            # The kernel's unlimited sentinel: a percentage of it is
            # meaningless, so report unavailable rather than "0% used".
            continue
        working_set = max(0, used - _inactive_file_bytes(base / "memory.stat"))
        return working_set, limit
    return None


def read_memory() -> tuple[int, int] | None:
    """Read container memory ``(working_set, limit)``; ``None`` if unusable."""
    root = Path(os.environ.get(CGROUP_ROOT_ENV, "/sys/fs/cgroup"))
    return _read_cgroup_v2(root) or _read_cgroup_v1(root)


@dataclass(frozen=True)
class CapacitySnapshot:
    """One observation of a host's admission state.

    ``available`` is ``None`` when no count limit is configured — an
    unknown number of free slots, never zero.
    """

    active: int
    pending: int
    limit: int | None
    available: int | None
    accepting: bool
    reason: str | None
    pressure: bool
    memory_used: int | None
    memory_limit: int | None
    memory_percent: float | None
    memory_high_threshold: float
    memory_resume_threshold: float
    reserve_mb: int
    observed_at: float

    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "pending": self.pending,
            "limit": self.limit,
            "available": self.available,
            "accepting": self.accepting,
            "reason": self.reason,
            "pressure": self.pressure,
            "memory_used": self.memory_used,
            "memory_limit": self.memory_limit,
            "memory_percent": self.memory_percent,
            "memory_high_threshold": self.memory_high_threshold,
            "memory_resume_threshold": self.memory_resume_threshold,
            "reserve_mb": self.reserve_mb,
            "observed_at": self.observed_at,
        }


class HostAdmission:
    """Thread-safe hard-count + memory-pressure gate for runner launches.

    :param active_source: Returns the number of live runners. Called while
        the admission lock is held so counting (and any dead-handle
        pruning the caller does inside it) is atomic with the decision.
    :param memory_reader: Injectable cgroup reader for tests.
    :param clock: Injectable time source for freshness assertions.
    """

    def __init__(
        self,
        active_source: Callable[[], int],
        *,
        memory_reader: Callable[[], tuple[int, int] | None] = read_memory,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._active_source = active_source
        self._memory_reader = memory_reader
        self._clock = clock
        self.max_runners = configured_max_runners()
        self.high = min(100.0, _positive_float(MEMORY_HIGH_ENV, DEFAULT_HIGH_PERCENT))
        self.resume = min(self.high, _positive_float(MEMORY_RESUME_ENV, DEFAULT_RESUME_PERCENT))
        self.reserve_mb = _nonnegative_int(RUNNER_RESERVE_ENV, DEFAULT_RESERVE_MB)
        # request_id → outstanding reservations. A count (not a set) keeps
        # accounting correct if a server ever retries a request id while the
        # first attempt is still in flight.
        self._pending: dict[str, int] = {}
        self._pressured = False
        self._lock = threading.RLock()

    @property
    def pending(self) -> int:
        with self._lock:
            return sum(self._pending.values())

    def reserve(self, request_id: str) -> CapacitySnapshot | None:
        """Atomically admit one launch, or return the refusing snapshot.

        On success the caller holds a reservation that MUST be released
        (success, failure, cancellation, or exception) via :meth:`release`.
        """
        with self._lock:
            snapshot = self._snapshot_locked()
            if not snapshot.accepting:
                return snapshot
            self._pending[request_id] = self._pending.get(request_id, 0) + 1
            return None

    def release(self, request_id: str) -> None:
        """Drop one reservation; unknown ids are a no-op."""
        with self._lock:
            remaining = self._pending.get(request_id)
            if remaining is None:
                return
            if remaining <= 1:
                del self._pending[request_id]
            else:
                self._pending[request_id] = remaining - 1

    def snapshot(self) -> CapacitySnapshot:
        """Return a fresh advisory snapshot (no reservation is taken)."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> CapacitySnapshot:
        active = self._active_source()
        pending = sum(self._pending.values())
        used, limit, percent = self._evaluate_memory_locked(pending)
        memory_reason = MEMORY_PRESSURE_REASON if self._pressured else None
        total = active + pending
        count_reason = (
            COUNT_LIMIT_REASON
            if self.max_runners is not None and total >= self.max_runners
            else None
        )
        # The count ceiling is the primary contract, so it names the reason
        # when both gates are closed.
        reason = count_reason or memory_reason
        return CapacitySnapshot(
            active=active,
            pending=pending,
            limit=self.max_runners,
            available=(None if self.max_runners is None else max(0, self.max_runners - total)),
            accepting=reason is None,
            reason=reason,
            pressure=self._pressured,
            memory_used=used,
            memory_limit=limit,
            memory_percent=percent,
            memory_high_threshold=self.high,
            memory_resume_threshold=self.resume,
            reserve_mb=self.reserve_mb,
            observed_at=self._clock(),
        )

    def _evaluate_memory_locked(
        self,
        pending: int,
    ) -> tuple[int | None, int | None, float | None]:
        """Update the pressure latch and report the reading behind it.

        ``pending`` launches have been admitted but have not yet grown into
        their memory, so the projection must reserve for them *plus* the one
        being considered. Reserving for a single runner would let a burst of
        concurrent launches all pass the same reading and overshoot the
        watermark together.
        """
        memory = self._memory_reader()
        if memory is None:
            # Explicit count-only fallback: no telemetry means no memory
            # opinion at all, never a synthetic "safe" or "unsafe" claim.
            self._pressured = False
            return None, None, None
        used, limit = memory
        percent = used * 100.0 / limit
        high_bytes = limit * self.high / 100.0
        needed = (pending + 1) * self.reserve_mb * 1024 * 1024
        reserve_fits = used + needed <= high_bytes
        if self._pressured:
            if percent <= self.resume and reserve_fits:
                self._pressured = False
        elif percent >= self.high or not reserve_fits:
            self._pressured = True
        return used, limit, percent
