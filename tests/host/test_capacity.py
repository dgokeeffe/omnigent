"""Deterministic tests for host runner admission (``omnigent.host.capacity``).

Covers the exact 10/11 boundary, concurrent barrier races, pending
accounting, duplicate request ids, release on every outcome, dead-slot
reuse, and every cgroup telemetry shape (finite / ``max`` / malformed /
absent) plus the reserve and hysteresis boundaries.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from omnigent.host.capacity import (
    COUNT_LIMIT_REASON,
    DEFAULT_HIGH_PERCENT,
    DEFAULT_RESERVE_MB,
    DEFAULT_RESUME_PERCENT,
    MAX_RUNNERS_ENV,
    MEMORY_HIGH_ENV,
    MEMORY_PRESSURE_REASON,
    MEMORY_RESUME_ENV,
    RUNNER_RESERVE_ENV,
    HostAdmission,
    configured_max_runners,
    read_memory,
)

MiB = 1024 * 1024


def _admission(
    active: int | list[int],
    *,
    memory: tuple[int, int] | None = None,
    memory_reader: object | None = None,
) -> HostAdmission:
    """Build an admission gate with an injected active count and memory.

    :param active: A fixed active count, or a one-element list used as a
        mutable cell so a test can change the count between calls.
    :param memory: Fixed ``(used, limit)`` bytes, or ``None`` for
        "telemetry unavailable".
    :param memory_reader: Overrides ``memory`` with a callable.
    """
    cell = active if isinstance(active, list) else [active]
    reader = memory_reader if memory_reader is not None else (lambda: memory)
    return HostAdmission(lambda: cell[0], memory_reader=reader)  # type: ignore[arg-type]


# ── configuration ────────────────────────────────────────────


def test_configured_max_runners_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positive value caps; 0/unset/garbage keeps the unlimited mode."""
    monkeypatch.delenv(MAX_RUNNERS_ENV, raising=False)
    assert configured_max_runners() is None
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    assert configured_max_runners() == 10
    monkeypatch.setenv(MAX_RUNNERS_ENV, "0")
    assert configured_max_runners() is None
    monkeypatch.setenv(MAX_RUNNERS_ENV, "not-a-number")
    assert configured_max_runners() is None


def test_canonical_memory_env_names_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed CoDA env names must be the ones the host actually reads.

    A mismatch here silently discards the deployment's memory policy and
    falls back to defaults, which is the defect this contract fixes.
    """
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    monkeypatch.setenv(MEMORY_HIGH_ENV, "85")
    monkeypatch.setenv(MEMORY_RESUME_ENV, "60")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "1024")
    gate = _admission(0, memory=None)
    assert (gate.high, gate.resume, gate.reserve_mb) == (85.0, 60.0, 1024)
    assert MEMORY_HIGH_ENV == "OMNIGENT_HOST_MEMORY_HIGH_WATERMARK_PERCENT"
    assert MEMORY_RESUME_ENV == "OMNIGENT_HOST_MEMORY_RESUME_THRESHOLD_PERCENT"
    assert RUNNER_RESERVE_ENV == "OMNIGENT_HOST_RUNNER_MEMORY_RESERVE_MB"


def test_malformed_memory_env_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage or non-positive policy values must not disable the gate."""
    monkeypatch.setenv(MEMORY_HIGH_ENV, "abc")
    monkeypatch.setenv(MEMORY_RESUME_ENV, "-5")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "oops")
    gate = _admission(0, memory=None)
    assert gate.high == DEFAULT_HIGH_PERCENT
    assert gate.resume == DEFAULT_RESUME_PERCENT
    assert gate.reserve_mb == DEFAULT_RESERVE_MB


def test_resume_is_clamped_to_high(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume threshold above the watermark is clamped, not honored."""
    monkeypatch.setenv(MEMORY_HIGH_ENV, "70")
    monkeypatch.setenv(MEMORY_RESUME_ENV, "95")
    gate = _admission(0, memory=None)
    assert gate.high == 70.0
    assert gate.resume == 70.0


def test_zero_reserve_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit 0 MB reserve disables only the reserve, not the watermark."""
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "0")
    gate = _admission(0, memory=(79 * MiB, 100 * MiB))
    assert gate.reserve_mb == 0
    assert gate.snapshot().accepting is True


# ── count ceiling ────────────────────────────────────────────


def test_tenth_launch_admitted_eleventh_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """With max=10 exactly ten reservations fit; the eleventh is refused."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    gate = _admission(0, memory=None)
    for i in range(10):
        assert gate.reserve(f"req-{i}") is None, f"launch {i} must be admitted"
    refused = gate.reserve("req-10")
    assert refused is not None
    assert refused.reason == COUNT_LIMIT_REASON
    assert refused.accepting is False
    assert refused.pending == 10
    assert refused.available == 0
    assert refused.limit == 10


def test_active_and_pending_share_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nine live runners leave exactly one launch slot, not ten."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    gate = _admission(9, memory=None)
    assert gate.reserve("a") is None
    refused = gate.reserve("b")
    assert refused is not None and refused.reason == COUNT_LIMIT_REASON
    assert (refused.active, refused.pending) == (9, 1)


def test_unlimited_mode_never_refuses_on_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset cap = unlimited; available is unknown (None), never zero."""
    monkeypatch.delenv(MAX_RUNNERS_ENV, raising=False)
    gate = _admission(50, memory=None)
    assert gate.reserve("x") is None
    snapshot = gate.snapshot()
    assert snapshot.limit is None
    assert snapshot.available is None
    assert snapshot.accepting is True


def test_release_frees_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A released reservation is immediately reusable."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "1")
    gate = _admission(0, memory=None)
    assert gate.reserve("a") is None
    assert gate.reserve("b") is not None
    gate.release("a")
    assert gate.pending == 0
    assert gate.reserve("b") is None


def test_release_of_unknown_id_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray release must not create negative capacity."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "2")
    gate = _admission(0, memory=None)
    assert gate.reserve("a") is None
    gate.release("never-reserved")
    gate.release("never-reserved")
    assert gate.pending == 1


def test_duplicate_request_id_counts_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replayed request id must consume a second slot, not be swallowed.

    Counting by id-set would make two in-flight launches look like one and
    let an eleventh runner through.
    """
    monkeypatch.setenv(MAX_RUNNERS_ENV, "2")
    gate = _admission(0, memory=None)
    assert gate.reserve("same") is None
    assert gate.reserve("same") is None
    assert gate.pending == 2
    assert gate.reserve("other") is not None
    # Each release drops exactly one.
    gate.release("same")
    assert gate.pending == 1
    gate.release("same")
    assert gate.pending == 0


def test_release_on_exception_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """The try/finally contract keeps a failing launch from leaking a slot."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "1")
    gate = _admission(0, memory=None)
    with pytest.raises(RuntimeError):
        assert gate.reserve("a") is None
        try:
            raise RuntimeError("spawn blew up")
        finally:
            gate.release("a")
    assert gate.pending == 0
    assert gate.reserve("b") is None


def test_dead_runner_slot_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the active source drops a dead runner, its slot admits again."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "1")
    live = [1]
    gate = _admission(live, memory=None)
    assert gate.reserve("a") is not None
    live[0] = 0
    assert gate.reserve("a") is None


def test_concurrent_barrier_race_admits_exactly_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eleven threads racing a max=10 gate must yield exactly ten admissions.

    All threads release simultaneously from a barrier so the reserve calls
    genuinely contend, which is the race a check-then-act gate loses.
    """
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    gate = _admission(0, memory=None)
    threads_count = 11
    barrier = threading.Barrier(threads_count)
    admitted: list[str] = []
    refused: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait()
        outcome = gate.reserve(f"req-{index}")
        with lock:
            (refused if outcome is not None else admitted).append(f"req-{index}")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 10
    assert len(refused) == 1
    assert gate.pending == 10
    assert gate.snapshot().available == 0


def test_concurrent_race_with_live_runners_never_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight live runners plus a 10-way race must admit only two."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    gate = _admission(8, memory=None)
    barrier = threading.Barrier(10)
    admitted: list[int] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait()
        if gate.reserve(f"req-{index}") is None:
            with lock:
                admitted.append(index)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(admitted) == 2


# ── cgroup telemetry ─────────────────────────────────────────


def _write_cgroup(root: Path, current: str, maximum: str) -> None:
    (root / "memory.current").write_text(current)
    (root / "memory.max").write_text(maximum)


def test_read_memory_finite_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A finite ``memory.max`` yields a usable (used, limit) pair."""
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    _write_cgroup(tmp_path, "1048576\n", "4194304\n")
    assert read_memory() == (1048576, 4194304)


def test_read_memory_unlimited_and_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max``, garbage, a zero limit, and missing files all read as unusable."""
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    _write_cgroup(tmp_path, "1048576", "max")
    assert read_memory() is None
    _write_cgroup(tmp_path, "not-a-number", "4194304")
    assert read_memory() is None
    _write_cgroup(tmp_path, "1048576", "0")
    assert read_memory() is None
    _write_cgroup(tmp_path, "1048576", "")
    assert read_memory() is None


def test_read_memory_absent_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside a cgroup (a laptop) telemetry is simply absent."""
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path / "nope"))
    assert read_memory() is None


def test_read_memory_subtracts_reclaimable_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usage is the working set, not usage-including-page-cache.

    Reclaimable file cache is not OOM risk; counting it would refuse
    launches on a container that is merely warm.
    """
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    _write_cgroup(tmp_path, str(80 * MiB), str(100 * MiB))
    (tmp_path / "memory.stat").write_text(f"anon 1000\ninactive_file {30 * MiB}\n")
    assert read_memory() == (50 * MiB, 100 * MiB)
    # A malformed / missing stat file simply subtracts nothing.
    (tmp_path / "memory.stat").write_text("inactive_file not-a-number\n")
    assert read_memory() == (80 * MiB, 100 * MiB)
    (tmp_path / "memory.stat").unlink()
    assert read_memory() == (80 * MiB, 100 * MiB)


def _write_cgroup_v1(base: Path, usage: str, limit: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "memory.usage_in_bytes").write_text(usage)
    (base / "memory.limit_in_bytes").write_text(limit)


def test_read_memory_falls_back_to_cgroup_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Databricks Apps containers expose cgroup v1, so v1 must work.

    Without this the memory gate is permanently inactive in production and
    only the count ceiling protects the container.
    """
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    _write_cgroup_v1(tmp_path / "memory", str(3 * MiB), str(8 * MiB))
    assert read_memory() == (3 * MiB, 8 * MiB)
    # v1 files may also sit directly at the configured root.
    other = tmp_path / "flat"
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(other))
    _write_cgroup_v1(other, str(2 * MiB), str(8 * MiB))
    assert read_memory() == (2 * MiB, 8 * MiB)


def test_cgroup_v2_wins_over_v1_when_both_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 is authoritative on a hybrid host."""
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    _write_cgroup(tmp_path, str(10 * MiB), str(100 * MiB))
    _write_cgroup_v1(tmp_path / "memory", str(90 * MiB), str(200 * MiB))
    assert read_memory() == (10 * MiB, 100 * MiB)


def test_cgroup_v1_unlimited_sentinel_reads_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2^63-ish v1 limit means unlimited, not "0% used".

    Treating the sentinel as a real budget would report a container as
    essentially empty and disable the memory gate silently.
    """
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    _write_cgroup_v1(tmp_path / "memory", str(3 * MiB), "9223372036854771712")
    assert read_memory() is None


def test_cgroup_v1_malformed_reads_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    _write_cgroup_v1(tmp_path / "memory", "not-a-number", str(8 * MiB))
    assert read_memory() is None
    _write_cgroup_v1(tmp_path / "memory", str(3 * MiB), "0")
    assert read_memory() is None
    _write_cgroup_v1(tmp_path / "memory", str(3 * MiB), "-5")
    assert read_memory() is None


def test_cgroup_v1_subtracts_reclaimable_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", str(tmp_path))
    base = tmp_path / "memory"
    _write_cgroup_v1(base, str(80 * MiB), str(100 * MiB))
    (base / "memory.stat").write_text(f"total_rss 100\ntotal_inactive_file {25 * MiB}\n")
    assert read_memory() == (55 * MiB, 100 * MiB)


def test_never_reads_host_wide_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without cgroup files the gate must not substitute machine memory.

    A host-wide reading is not the container's budget, so the documented
    behavior is count-only admission with no memory opinion at all.
    """
    monkeypatch.setenv("OMNIGENT_CGROUP_ROOT", "/definitely/not/a/cgroup")
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    gate = HostAdmission(lambda: 0)
    snapshot = gate.snapshot()
    assert snapshot.memory_used is None
    assert snapshot.memory_limit is None
    assert snapshot.memory_percent is None
    assert snapshot.accepting is True
    assert snapshot.reason is None


def test_unavailable_telemetry_falls_back_to_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no telemetry the count ceiling is the only limit."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "2")
    gate = _admission(2, memory=None)
    refused = gate.reserve("a")
    assert refused is not None
    assert refused.reason == COUNT_LIMIT_REASON
    assert refused.memory_percent is None


# ── memory pressure ─────────────────────────────────────────


def test_reserve_boundary_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reserve landing exactly on the watermark still fits."""
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "10")
    # 70 MiB used + 10 MiB reserve == 80% of 100 MiB, i.e. exactly the line.
    exact = _admission(0, memory=(70 * MiB, 100 * MiB))
    assert exact.snapshot().accepting is True
    # One byte more crosses it.
    over = _admission(0, memory=(70 * MiB + 1, 100 * MiB))
    snapshot = over.snapshot()
    assert snapshot.accepting is False
    assert snapshot.reason == MEMORY_PRESSURE_REASON


def test_high_watermark_alone_latches_pressure(monkeypatch: pytest.MonkeyPatch) -> None:
    """At/above the watermark the gate closes even with a zero reserve."""
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "0")
    gate = _admission(0, memory=(80 * MiB, 100 * MiB))
    assert gate.snapshot().reason == MEMORY_PRESSURE_REASON


def test_hysteresis_holds_until_resume_and_reserve_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressure clears only below the resume threshold AND with room to spare.

    The set and clear conditions are exact complements, so one evaluation
    can never clear and immediately re-arm the latch (which would let a
    launch through above the watermark).
    """
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(MEMORY_RESUME_ENV, "70")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "5")
    reading = [(50 * MiB, 100 * MiB)]
    gate = _admission(0, memory_reader=lambda: reading[0])
    assert gate.snapshot().accepting is True

    # Cross the watermark → latched.
    reading[0] = (82 * MiB, 100 * MiB)
    assert gate.snapshot().reason == MEMORY_PRESSURE_REASON
    # Below the watermark but above resume → still latched (hysteresis).
    reading[0] = (75 * MiB, 100 * MiB)
    assert gate.snapshot().reason == MEMORY_PRESSURE_REASON
    # At resume but the 5 MiB reserve would land on 80% exactly → clears.
    reading[0] = (70 * MiB, 100 * MiB)
    first = gate.snapshot()
    assert first.accepting is True
    # ...and stays clear on the next evaluation: no flapping.
    assert gate.snapshot().accepting is True


def test_resume_below_threshold_but_reserve_too_big_stays_latched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reserve larger than the resume headroom keeps the gate closed."""
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(MEMORY_RESUME_ENV, "70")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "20")
    reading = [(85 * MiB, 100 * MiB)]
    gate = _admission(0, memory_reader=lambda: reading[0])
    assert gate.snapshot().reason == MEMORY_PRESSURE_REASON
    # 65% is under the 70% resume threshold, but 65 + 20 = 85% > 80%.
    reading[0] = (65 * MiB, 100 * MiB)
    assert gate.snapshot().reason == MEMORY_PRESSURE_REASON
    # 60 + 20 = 80% exactly → fits, so it clears.
    reading[0] = (60 * MiB, 100 * MiB)
    assert gate.snapshot().accepting is True


def test_memory_pressure_refuses_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressure refuses a launch even when the count ceiling has room."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "0")
    gate = _admission(0, memory=(90 * MiB, 100 * MiB))
    refused = gate.reserve("a")
    assert refused is not None
    assert refused.reason == MEMORY_PRESSURE_REASON
    assert refused.available == 10  # count slots exist; memory is the blocker
    assert gate.pending == 0, "a refused launch must not hold a reservation"


def test_count_reason_wins_when_both_gates_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard ceiling is the primary contract, so it names the reason."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "1")
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "0")
    gate = _admission(1, memory=(95 * MiB, 100 * MiB))
    refused = gate.reserve("a")
    assert refused is not None
    assert refused.reason == COUNT_LIMIT_REASON


# ── snapshot shape ───────────────────────────────────────────


def test_snapshot_as_dict_is_wire_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """as_dict must carry every field the protocol/API projection needs."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(MEMORY_RESUME_ENV, "70")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "768")
    gate = HostAdmission(
        lambda: 3,
        # 1 GiB used of 8 GiB, so the 768 MiB reserve still fits under 80%.
        memory_reader=lambda: (1024 * MiB, 8192 * MiB),
        clock=lambda: 1234.5,
    )
    assert gate.reserve("in-flight") is None
    payload = gate.snapshot().as_dict()
    assert payload == {
        "active": 3,
        "pending": 1,
        "limit": 10,
        "available": 6,
        "accepting": True,
        "reason": None,
        "pressure": False,
        "memory_used": 1024 * MiB,
        "memory_limit": 8192 * MiB,
        "memory_percent": 12.5,
        "memory_high_threshold": 80.0,
        "memory_resume_threshold": 70.0,
        "reserve_mb": 768,
        "observed_at": 1234.5,
    }


# ── review-driven regression coverage ────────────────────────


def test_pending_launches_each_reserve_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst must not admit N launches against a single reserve.

    Reserving for only one runner let ten concurrent launches all pass the
    same reading and overshoot the watermark together.
    """
    monkeypatch.setenv(MAX_RUNNERS_ENV, "10")
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "10")
    # 50 MiB used of 100 MiB: one 10 MiB reserve fits (60 <= 80), three do not.
    gate = _admission(0, memory=(50 * MiB, 100 * MiB))
    assert gate.reserve("a") is None
    assert gate.reserve("b") is None
    assert gate.reserve("c") is None
    refused = gate.reserve("d")
    assert refused is not None
    assert refused.reason == MEMORY_PRESSURE_REASON
    assert refused.pressure is True


def test_snapshot_reports_pressure_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pressure` is a first-class field, not something to infer from `reason`."""
    monkeypatch.setenv(MAX_RUNNERS_ENV, "1")
    monkeypatch.setenv(MEMORY_HIGH_ENV, "80")
    monkeypatch.setenv(RUNNER_RESERVE_ENV, "0")
    # Count-limited but memory-healthy: refusing for capacity is not pressure.
    healthy = _admission(1, memory=(10 * MiB, 100 * MiB))
    snapshot = healthy.snapshot()
    assert snapshot.accepting is False
    assert snapshot.reason == COUNT_LIMIT_REASON
    assert snapshot.pressure is False
    # Memory-limited: pressure is set even though the reason names it too.
    pressured = _admission(0, memory=(95 * MiB, 100 * MiB))
    assert pressured.snapshot().pressure is True
    # Telemetry absent: no memory opinion at all.
    unknown = _admission(0, memory=None)
    assert unknown.snapshot().pressure is False
