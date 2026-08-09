"""Host daemon launch admission: refusal, cleanup, and publication.

Complements ``tests/host/test_capacity.py`` (the gate in isolation) by
covering the daemon boundary: refusal happens *before* the fork, every
outcome releases the reservation, dead runners free their slot, duplicate
runner ids never orphan a live process, and a fresh snapshot is published
on launch / stop / exit and in hello.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.host.capacity import MEMORY_PRESSURE_REASON
from omnigent.host.connect import HostProcess, _RunnerHandle
from omnigent.host.frames import (
    HOST_AT_CAPACITY_ERROR_CODE,
    HostCapacityUpdateFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostStopRunnerFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.runner.identity import token_bound_runner_id

MiB = 1024 * 1024


class _FakeWs:
    """Minimal websocket capturing every frame the daemon sends."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    def capacity_frames(self) -> list[HostCapacityUpdateFrame]:
        frames = []
        for raw in self.sent:
            if json.loads(raw).get("kind") == "host.capacity_update":
                decoded = decode_host_frame(raw)
                assert isinstance(decoded, HostCapacityUpdateFrame)
                frames.append(decoded)
        return frames


class _FakeProc:
    """A process handle that never dies unless told to."""

    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _host(ws: _FakeWs | None = None) -> HostProcess:
    host = HostProcess(
        identity=HostIdentity(host_id="host_capacity_test", name="test-laptop"),
        server_url="http://localhost:8000",
    )
    if ws is not None:
        host._ws = ws
    return host


def _fill_runners(host: HostProcess, count: int, *, dead: int = 0) -> None:
    """Register ``count`` handles, the last ``dead`` of them already exited."""
    for i in range(count):
        returncode = 0 if i >= count - dead else None
        host._runners[f"runner_{i}"] = _RunnerHandle(
            proc=_FakeProc(pid=1000 + i, returncode=returncode),  # type: ignore[arg-type]
            log_path=Path("/tmp/runner.log"),
        )


def _launch_frame(request_id: str, workspace: Path, token: str) -> HostLaunchRunnerFrame:
    return HostLaunchRunnerFrame(
        request_id=request_id,
        binding_token=token,
        workspace=str(workspace),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


# ── refusal before spawn ─────────────────────────────────────


async def test_eleventh_launch_refused_before_any_spawn(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full host refuses with a typed code without paying for a fork.

    Reserving before the spawn is the whole point: an 11th fork on an
    already-saturated container is what causes the OOM this guards.
    """
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    ws = _FakeWs()
    host = _host(ws)
    _fill_runners(host, 10)

    with patch("omnigent.host.connect.subprocess.Popen") as popen:
        result = await host._handle_launch(_launch_frame("req-11", workspace, "tok-11"))

    popen.assert_not_called()
    assert isinstance(result, HostLaunchRunnerResultFrame)
    assert result.status == "failed"
    assert result.error_code == HOST_AT_CAPACITY_ERROR_CODE
    assert result.error is not None
    # The message must name the actionable next step, not just the state.
    assert "10 running" in result.error
    assert "another host" in result.error
    # A refusal still publishes so the server's advisory snapshot converges.
    assert ws.capacity_frames(), "refusal must publish a capacity snapshot"
    assert ws.capacity_frames()[-1].capacity.accepting is False
    assert host._admission.pending == 0, "refused launch must hold no reservation"


async def test_memory_pressure_refuses_before_spawn(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory pressure refuses even with count slots free, and says why."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    monkeypatch.setenv("OMNIGENT_HOST_MEMORY_HIGH_WATERMARK_PERCENT", "80")
    monkeypatch.setenv("OMNIGENT_HOST_RUNNER_MEMORY_RESERVE_MB", "0")
    host = _host(_FakeWs())
    host._admission._memory_reader = lambda: (95 * MiB, 100 * MiB)  # type: ignore[assignment]

    with patch("omnigent.host.connect.subprocess.Popen") as popen:
        result = await host._handle_launch(_launch_frame("req-1", workspace, "tok-1"))

    popen.assert_not_called()
    assert result.error_code == HOST_AT_CAPACITY_ERROR_CODE
    assert result.error is not None and "memory" in result.error.lower()
    assert host._admission.snapshot().reason == MEMORY_PRESSURE_REASON


async def test_dead_runner_frees_its_slot(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exited runner must not hold capacity until its watcher notices."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "2")
    host = _host(_FakeWs())
    _fill_runners(host, 2, dead=1)

    spawned = _FakeProc(pid=7777)
    with patch.object(
        host,
        "_spawn_runner_proc",
        return_value=(spawned, Path("/tmp/runner-new.log")),
    ):
        result = await host._handle_launch(_launch_frame("req-1", workspace, "tok-new"))

    assert result.status == "launched"
    # The exited handle still exists (its watcher owns removal and the crash
    # report), but it no longer consumes a slot: only runner_0 and the new
    # runner count toward the ceiling of 2.
    assert token_bound_runner_id("tok-new") in host._runners
    assert "runner_1" in host._runners, "the watcher, not capacity, removes handles"
    assert host._admission.snapshot().active == 2
    for task in host._watcher_tasks:
        task.cancel()


# ── reservation release on every outcome ─────────────────────


async def test_successful_launch_releases_pending_and_keeps_active(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a launch the slot is accounted as active, never double-counted."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    host = _host(_FakeWs())
    with patch.object(
        host,
        "_spawn_runner_proc",
        return_value=(_FakeProc(pid=555), Path("/tmp/runner.log")),
    ):
        result = await host._handle_launch(_launch_frame("req-1", workspace, "tok-1"))
    assert result.status == "launched"
    snapshot = host._admission.snapshot()
    assert (snapshot.active, snapshot.pending) == (1, 0)
    assert snapshot.available == 9
    for task in host._watcher_tasks:
        task.cancel()


async def test_spawn_failure_releases_pending(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed spawn must free its reservation or capacity leaks to zero."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    with patch.object(host, "_spawn_runner_proc", side_effect=OSError("no fds")):
        result = await host._handle_launch(_launch_frame("req-1", workspace, "tok-1"))
    assert result.status == "failed"
    assert host._admission.pending == 0
    assert host._admission.snapshot().accepting is True


async def test_bad_workspace_refusal_releases_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-spawn validation refusal also releases its reservation."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    missing = tmp_path / "gone"
    result = await host._handle_launch(_launch_frame("req-1", missing, "tok-1"))
    assert result.status == "failed"
    assert result.error_code != HOST_AT_CAPACITY_ERROR_CODE
    assert host._admission.pending == 0


async def test_cancelled_launch_releases_pending(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a launch mid-spawn must not strand its reservation."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    started = asyncio.Event()

    def _slow_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        started.set()
        import time

        time.sleep(0.5)
        return _FakeProc(pid=999), Path("/tmp/runner.log")

    with patch.object(host, "_spawn_runner_proc", side_effect=_slow_spawn):
        task = asyncio.create_task(host._handle_launch(_launch_frame("req-1", workspace, "tok-1")))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The reservation is deliberately retained until the abandoned child is
        # torn down (see test_cancelled_launch_holds_the_slot_until_the_orphan_dies),
        # then released — so it must not strand.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if host._admission.pending == 0:
                break

    assert host._admission.pending == 0
    assert host._admission.snapshot().accepting is True


async def test_concurrent_launches_respect_the_ceiling(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three simultaneous launches against a max of 2 admit exactly two.

    The spawns are slow on purpose so all three requests overlap, which is
    the race a post-spawn-only check loses.
    """
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "2")
    host = _host(_FakeWs())
    counter = [0]

    def _slow_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        import time

        time.sleep(0.2)
        counter[0] += 1
        return _FakeProc(pid=8000 + counter[0]), Path("/tmp/runner.log")

    with patch.object(host, "_spawn_runner_proc", side_effect=_slow_spawn):
        results = await asyncio.gather(
            host._handle_launch(_launch_frame("r1", workspace, "t1")),
            host._handle_launch(_launch_frame("r2", workspace, "t2")),
            host._handle_launch(_launch_frame("r3", workspace, "t3")),
        )

    launched = [r for r in results if r.status == "launched"]
    refused = [r for r in results if r.error_code == HOST_AT_CAPACITY_ERROR_CODE]
    assert len(launched) == 2
    assert len(refused) == 1
    assert counter[0] == 2, "the refused launch must never have spawned"
    assert len(host._runners) == 2
    for task in host._watcher_tasks:
        task.cancel()


# ── duplicate runner ids ─────────────────────────────────────


async def test_duplicate_runner_id_refused_before_spawn(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replayed binding token must not overwrite (and orphan) a live runner."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    host = _host(_FakeWs())
    live = _FakeProc(pid=111)
    runner_id = token_bound_runner_id("tok-dup")
    host._runners[runner_id] = _RunnerHandle(proc=live, log_path=Path("/tmp/a.log"))  # type: ignore[arg-type]

    with patch.object(host, "_spawn_runner_proc") as spawn:
        result = await host._handle_launch(_launch_frame("req-dup", workspace, "tok-dup"))

    spawn.assert_not_called()
    assert result.status == "failed"
    assert result.error is not None and "already running" in result.error
    assert host._runners[runner_id].proc is live
    assert live.terminated is False
    assert host._admission.pending == 0


async def test_duplicate_runner_id_after_spawn_kills_the_loser(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-spawn id collision terminates the new child, not the winner."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    host = _host(_FakeWs())
    runner_id = token_bound_runner_id("tok-dup")
    winner = _FakeProc(pid=111)
    loser = _FakeProc(pid=222)

    def _spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        # Simulate the winner registering while this spawn was in flight.
        host._runners[runner_id] = _RunnerHandle(  # type: ignore[arg-type]
            proc=winner,
            log_path=Path("/tmp/a.log"),
        )
        return loser, Path("/tmp/b.log")

    with patch.object(host, "_spawn_runner_proc", side_effect=_spawn):
        result = await host._handle_launch(_launch_frame("req-dup", workspace, "tok-dup"))

    assert result.status == "failed"
    assert loser.terminated is True, "the speculative child must be reaped"
    assert winner.terminated is False
    assert host._runners[runner_id].proc is winner
    assert host._admission.pending == 0


# ── capacity publication ─────────────────────────────────────


async def test_launch_publishes_updated_capacity(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a launch the published snapshot shows the new active count."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    ws = _FakeWs()
    host = _host(ws)
    with patch.object(
        host,
        "_spawn_runner_proc",
        return_value=(_FakeProc(pid=1), Path("/tmp/runner.log")),
    ):
        await host._handle_launch(_launch_frame("req-1", workspace, "tok-1"))
    frames = ws.capacity_frames()
    assert frames, "a launch must publish capacity"
    assert frames[-1].capacity.active == 1
    assert frames[-1].capacity.pending == 0
    assert frames[-1].capacity.limit == 10
    assert frames[-1].capacity.available == 9
    for task in host._watcher_tasks:
        task.cancel()


async def test_stop_publishes_freed_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping a runner frees its slot in the published snapshot."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    ws = _FakeWs()
    host = _host(ws)
    _fill_runners(host, 3)
    result = await host._handle_stop(HostStopRunnerFrame(request_id="s1", runner_id="runner_0"))
    assert result.status == "stopped"
    frames = ws.capacity_frames()
    assert frames[-1].capacity.active == 2
    assert frames[-1].capacity.available == 8


async def test_clean_runner_exit_publishes_freed_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that exits on its own releases its slot without a stop frame."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "2")
    ws = _FakeWs()
    host = _host(ws)
    proc = _FakeProc(pid=1, returncode=None)
    host._runners["runner_x"] = _RunnerHandle(proc=proc, log_path=Path("/tmp/x.log"))  # type: ignore[arg-type]
    watcher = asyncio.create_task(host._watch_runner("runner_x"))
    await asyncio.sleep(0)
    proc.returncode = 0
    await asyncio.wait_for(watcher, timeout=10)
    assert "runner_x" not in host._runners
    frames = ws.capacity_frames()
    assert frames[-1].capacity.active == 0
    assert frames[-1].capacity.accepting is True


async def test_publish_survives_a_dead_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """A send failure while publishing must never break launch handling."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")

    class _BrokenWs:
        async def send(self, raw: str) -> None:
            raise ConnectionError("tunnel gone")

    host = _host()
    host._ws = _BrokenWs()  # type: ignore[assignment]
    await host._publish_capacity()  # must not raise


async def test_publish_without_transport_is_a_noop() -> None:
    """Before the first connect there is nowhere to publish."""
    host = _host()
    host._ws = None
    await host._publish_capacity()


async def test_hello_carries_the_current_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect republishes capacity in hello so a new replica learns it."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    host = _host()
    _fill_runners(host, 4)
    frame = host._capacity_frame()
    hello = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="test-laptop",
        capacity=frame.capacity,
    )
    decoded = decode_host_frame(encode_host_frame(hello))
    assert isinstance(decoded, HostHelloFrame)
    assert decoded.capacity is not None
    assert decoded.capacity.active == 4
    assert decoded.capacity.limit == 10
    assert decoded.capacity.available == 6


async def test_uncapped_host_publishes_unknown_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local daemon with no cap reports null limit/available, not zero."""
    monkeypatch.delenv("OMNIGENT_HOST_MAX_RUNNERS", raising=False)
    host = _host()
    _fill_runners(host, 3)
    snapshot = host._capacity_frame().capacity
    assert snapshot.limit is None
    assert snapshot.available is None
    assert snapshot.accepting is True


async def test_launch_after_stop_reuses_the_freed_slot(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host at its ceiling accepts again as soon as a runner stops."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    _fill_runners(host, 1)
    refused = await host._handle_launch(_launch_frame("req-1", workspace, "tok-1"))
    assert refused.error_code == HOST_AT_CAPACITY_ERROR_CODE

    await host._handle_stop(HostStopRunnerFrame(request_id="s1", runner_id="runner_0"))
    with patch.object(
        host,
        "_spawn_runner_proc",
        return_value=(_FakeProc(pid=2), Path("/tmp/runner.log")),
    ):
        admitted = await host._handle_launch(_launch_frame("req-2", workspace, "tok-2"))
    assert admitted.status == "launched"
    for task in host._watcher_tasks:
        task.cancel()


def test_spawn_is_never_reached_when_refused(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: no real subprocess module call on the refusal path."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    _fill_runners(host, 1)
    real_popen = subprocess.Popen

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Popen must not be called for a refused launch")

    subprocess.Popen = _explode  # type: ignore[assignment]
    try:
        result = asyncio.run(host._handle_launch(_launch_frame("r", workspace, "t")))
    finally:
        subprocess.Popen = real_popen  # type: ignore[assignment]
    assert result.error_code == HOST_AT_CAPACITY_ERROR_CODE


# ── review-driven regression coverage ────────────────────────


async def test_capacity_count_does_not_suppress_a_crash_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing capacity must not steal the exit from the crash watcher.

    Pruning the handle inside the capacity count made ``_watch_runner`` read
    the removal as an intentional stop, so a crashed runner produced NO
    ``host.runner_exited`` frame and the waiting session timed out with a
    generic error instead of the real cause.
    """
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    ws = _FakeWs()
    host = _host(ws)
    proc = _FakeProc(pid=99, returncode=None)
    host._runners["runner_crash"] = _RunnerHandle(  # type: ignore[arg-type]
        proc=proc,
        log_path=Path("/tmp/crash.log"),
    )
    watcher = asyncio.create_task(host._watch_runner("runner_crash"))
    await asyncio.sleep(0)
    # The runner dies, and a capacity snapshot is taken BEFORE the watcher
    # observes it (the 15 s publish loop, a stop, or another launch).
    proc.returncode = 3
    host._capacity_frame()
    assert "runner_crash" in host._runners, "capacity must not remove the handle"
    await asyncio.wait_for(watcher, timeout=10)
    kinds = [json.loads(raw).get("kind") for raw in ws.sent]
    assert "host.runner_exited" in kinds, f"crash report missing; sent {kinds}"
    assert "runner_crash" not in host._runners
    assert ws.capacity_frames()[-1].capacity.active == 0


async def test_capacity_count_never_polls_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counting must not call poll(): for a zygote runner that blocks the loop.

    ``_handle_stop``/``_watch_runner`` deliberately run poll() off the event
    loop; the capacity path runs inline, so it may only read the already-known
    returncode.
    """
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "10")
    host = _host(_FakeWs())

    class _BlockingProc(_FakeProc):
        def poll(self) -> int | None:
            raise AssertionError("capacity must not call poll()")

    host._runners["runner_zygote"] = _RunnerHandle(  # type: ignore[arg-type]
        proc=_BlockingProc(pid=7),
        log_path=Path("/tmp/z.log"),
    )
    assert host._capacity_frame().capacity.active == 1


async def test_cancelled_launch_holds_the_slot_until_the_orphan_dies(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled launch keeps its reservation through orphan teardown.

    Releasing at cancellation let the next launch take the slot while the
    abandoned child was still alive, so a max of 1 could run two runners.
    """
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    started = asyncio.Event()
    orphan = _FakeProc(pid=4242)
    release_teardown = threading.Event()

    def _slow_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        started.set()
        time.sleep(0.3)
        return orphan, Path("/tmp/orphan.log")

    def _slow_stop(proc: object) -> None:
        release_teardown.wait(timeout=5)
        assert isinstance(proc, _FakeProc)
        proc.kill()

    with (
        patch.object(host, "_spawn_runner_proc", side_effect=_slow_spawn),
        patch.object(host, "_stop_runner_proc", side_effect=_slow_stop),
    ):
        task = asyncio.create_task(host._handle_launch(_launch_frame("req-1", workspace, "tok-1")))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Spawn lands, teardown is in flight and holding the reservation.
        for _ in range(60):
            await asyncio.sleep(0.05)
            if host._admission.pending == 1:
                break
        assert host._admission.pending == 1, "reservation must survive cancellation"
        assert host._admission.snapshot().accepting is False, (
            "the slot must not be reusable while the orphan is alive"
        )
        release_teardown.set()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if host._admission.pending == 0:
                break

    assert orphan.terminated is True, "the orphan must be torn down"
    assert host._admission.pending == 0
    assert host._admission.snapshot().accepting is True


async def test_cancelled_launch_with_failed_spawn_releases_immediately(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was created, so the slot frees at once."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    started = asyncio.Event()

    def _failing_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        started.set()
        time.sleep(0.2)
        raise OSError("no fds")

    with patch.object(host, "_spawn_runner_proc", side_effect=_failing_spawn):
        task = asyncio.create_task(host._handle_launch(_launch_frame("req-1", workspace, "tok-1")))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(60):
            await asyncio.sleep(0.05)
            if host._admission.pending == 0:
                break
    assert host._admission.pending == 0


async def test_replayed_request_id_cannot_free_an_orphans_slot(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replayed launch id must not release another launch's reservation.

    Tracking the hand-off in a set keyed by request id let a retry with the
    same id clear the cancelled launch's marker, so the cancelled launch's
    own finally released the reservation while its orphan child was still
    being torn down \u2014 admitting a runner past the hard ceiling.
    """
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "2")
    host = _host(_FakeWs())
    started = asyncio.Event()
    orphan = _FakeProc(pid=5150)
    hold = threading.Event()

    def _slow_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        started.set()
        time.sleep(0.3)
        return orphan, Path("/tmp/orphan.log")

    def _slow_stop(proc: object) -> None:
        hold.wait(timeout=5)
        assert isinstance(proc, _FakeProc)
        proc.kill()

    with (
        patch.object(host, "_spawn_runner_proc", side_effect=_slow_spawn),
        patch.object(host, "_stop_runner_proc", side_effect=_slow_stop),
    ):
        first = asyncio.create_task(
            host._handle_launch(_launch_frame("same-id", workspace, "tok-1"))
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        for _ in range(60):
            await asyncio.sleep(0.05)
            if host._admission.pending == 1:
                break
        assert host._admission.pending == 1

        # Replay the SAME request id while the orphan teardown still holds it.
        started.clear()
        second_proc = _FakeProc(pid=5151)

        def _second_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
            started.set()
            return second_proc, Path("/tmp/second.log")

        with patch.object(host, "_spawn_runner_proc", side_effect=_second_spawn):
            replay = await host._handle_launch(_launch_frame("same-id", workspace, "tok-2"))
        assert replay.status == "launched"
        # The orphan's reservation must STILL be held, so the host is full:
        # one live runner + one orphan reservation against a limit of 2.
        assert host._admission.pending == 1, "the orphan's reservation was stolen"
        assert host._admission.snapshot().accepting is False
        hold.set()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if host._admission.pending == 0:
                break

    assert orphan.terminated is True
    assert host._admission.pending == 0
    for task in host._watcher_tasks:
        task.cancel()


async def test_orphan_teardown_failure_uses_direct_kill_and_frees_the_slot(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graceful teardown failure falls back to kill before capacity frees."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    started = asyncio.Event()
    orphan = _FakeProc(pid=6060)

    def _slow_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        started.set()
        time.sleep(0.2)
        return orphan, Path("/tmp/orphan.log")

    def _boom(_proc: object) -> None:
        raise RuntimeError("zygote control socket wedged")

    with (
        patch.object(host, "_spawn_runner_proc", side_effect=_slow_spawn),
        patch.object(host, "_stop_runner_proc", side_effect=_boom),
        patch("os.kill", return_value=None),
    ):
        task = asyncio.create_task(
            host._handle_launch(_launch_frame("req-boom", workspace, "tok-1"))
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            await asyncio.sleep(0.05)
            if host._admission.pending == 0:
                break
    assert orphan.terminated is True, "the direct kill fallback must stop the orphan"
    assert host._admission.pending == 0
    assert host._admission.snapshot().accepting is True


async def test_cancelled_teardown_future_retries_and_releases(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor cancellation must not skip cleanup or strand capacity."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    assert host._admission.reserve("req-cancelled") is None
    orphan = _FakeProc(pid=6061)
    spawn = asyncio.get_running_loop().create_future()
    spawn.set_result((orphan, Path("/tmp/orphan.log")))
    teardown = asyncio.get_running_loop().create_future()
    teardown.cancel()

    with patch.object(asyncio.get_running_loop(), "run_in_executor", return_value=teardown):
        host._discard_abandoned_spawn(spawn, request_id="req-cancelled")
        await asyncio.sleep(0)

    assert host._admission.pending == 1
    for _ in range(100):
        await asyncio.sleep(0.05)
        if host._admission.pending == 0:
            break
    assert orphan.terminated is True
    assert host._admission.pending == 0


async def test_duplicate_runner_teardown_uses_direct_kill_fallback(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-spawn duplicate loser cannot leak when graceful stop raises."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "2")
    host = _host(_FakeWs())
    loser = _FakeProc(pid=6062)
    runner_id = token_bound_runner_id("tok-dup")

    def _racing_spawn(*_args: object, **_kwargs: object) -> tuple[_FakeProc, Path]:
        host._runners[runner_id] = _RunnerHandle(
            proc=_FakeProc(pid=6063), log_path=Path("/tmp/winner.log")
        )  # type: ignore[arg-type]
        return loser, Path("/tmp/loser.log")

    async def _executor(func: object, *args: object, **kwargs: object) -> object:
        if func == host._stop_untracked_runner_proc:
            raise RuntimeError("executor shut down")
        assert callable(func)
        return func(*args, **kwargs)

    with (
        patch.object(host, "_spawn_runner_proc", side_effect=_racing_spawn),
        patch.object(host, "_stop_runner_proc", side_effect=RuntimeError("zygote unavailable")),
        patch("os.kill", return_value=None),
        patch("asyncio.to_thread", new=_executor),
    ):
        result = await host._handle_launch(
            _launch_frame("req-duplicate-race", workspace, "tok-dup")
        )

    assert result.status == "failed"
    assert loser.terminated is True
    assert host._admission.pending == 0


async def test_executor_shutdown_fallback_retries_inline(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shut-down executor cannot permanently strand a recoverable orphan."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    assert host._admission.reserve("req-executor-down") is None
    orphan = _FakeProc(pid=6065)
    spawn = asyncio.get_running_loop().create_future()
    spawn.set_result((orphan, Path("/tmp/orphan.log")))

    with (
        patch.object(host, "_stop_untracked_runner_proc", side_effect=[False, True]),
        patch.object(
            asyncio.get_running_loop(),
            "run_in_executor",
            side_effect=RuntimeError("executor shut down"),
        ),
    ):
        host._discard_abandoned_spawn(spawn, request_id="req-executor-down")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if host._admission.pending == 0:
                break

    assert host._admission.pending == 0
    assert not host._untracked_runners


async def test_unconfirmed_orphan_keeps_capacity_reserved(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every kill path fails, safety wins over admitting past the ceiling."""
    monkeypatch.setenv("OMNIGENT_HOST_MAX_RUNNERS", "1")
    host = _host(_FakeWs())
    assert host._admission.reserve("req-unkillable") is None

    class _Unkillable(_FakeProc):
        def kill(self) -> None:
            raise PermissionError("denied")

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("runner", timeout)

    orphan = _Unkillable(pid=6064)
    spawn = asyncio.get_running_loop().create_future()
    spawn.set_result((orphan, Path("/tmp/orphan.log")))

    def _kill(pid: int, sig: int) -> None:
        assert pid == orphan.pid
        if sig != 0:
            raise PermissionError("denied")

    with (
        patch.object(host, "_stop_runner_proc", side_effect=RuntimeError("wedged")),
        patch("os.kill", side_effect=_kill),
    ):
        host._discard_abandoned_spawn(spawn, request_id="req-unkillable")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if host._untracked_runners:
                break

    assert host._admission.pending == 1
    assert host._admission.snapshot().accepting is False
    assert host._untracked_runners
    for task in host._watcher_tasks:
        task.cancel()
    host._untracked_runners.clear()
    host._admission.release("req-unkillable")
