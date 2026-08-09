"""Advisory server-side capacity preflight (``_host_launch``).

The host daemon's admission check is authoritative; this preflight only
short-circuits a launch that a FRESH snapshot already says will fail, so it
must let unknown or stale capacity through untouched.
"""

from __future__ import annotations

import time

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._host_launch import (
    CAPACITY_SNAPSHOT_MAX_AGE_S,
    CAPACITY_SNAPSHOT_MAX_SKEW_S,
    capacity_snapshot_is_fresh,
    refuse_if_at_capacity,
)


class _Registry:
    """Stands in for HostRegistry.capacity_snapshot only."""

    def __init__(self, snapshot: dict[str, object] | None) -> None:
        self._snapshot = snapshot

    def capacity_snapshot(
        self,
        host_id: str,
        workspace_id: int | None = None,
    ) -> dict[str, object] | None:
        return self._snapshot


def test_capacity_freshness_window() -> None:
    """Only a snapshot with a recent observed_at may drive a refusal."""
    assert capacity_snapshot_is_fresh(None) is False
    assert capacity_snapshot_is_fresh({}) is False
    assert capacity_snapshot_is_fresh({"accepting": False}) is False
    assert capacity_snapshot_is_fresh({"observed_at": "soon"}) is False
    assert capacity_snapshot_is_fresh({"observed_at": True}) is False
    assert capacity_snapshot_is_fresh({"observed_at": 100.0}, now=100.0) is True
    edge = 100.0 + CAPACITY_SNAPSHOT_MAX_AGE_S
    assert capacity_snapshot_is_fresh({"observed_at": 100.0}, now=edge) is True
    assert capacity_snapshot_is_fresh({"observed_at": 100.0}, now=edge + 1) is False


def test_preflight_passes_on_unknown_or_stale_capacity() -> None:
    """Unknown/stale capacity must never block a launch.

    The snapshot is advisory; only the host's own gate is authoritative, so
    a partially-upgraded fleet must keep working.
    """

    for snapshot in (
        None,
        {"accepting": False},  # no observed_at → unknown freshness
        {"accepting": False, "observed_at": 0.0},  # ancient
        {"accepting": True, "observed_at": None},
    ):
        refuse_if_at_capacity(host_registry=_Registry(snapshot), host_id="h")  # type: ignore[arg-type]


def test_preflight_refuses_on_fresh_not_accepting() -> None:
    """A fresh "not accepting" snapshot short-circuits with the typed 429."""
    count_full = {
        "accepting": False,
        "reason": "host_at_capacity",
        "active": 10,
        "limit": 10,
        "observed_at": time.time(),
    }
    with pytest.raises(OmnigentError) as excinfo:
        refuse_if_at_capacity(host_registry=_Registry(count_full), host_id="h")  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.HOST_AT_CAPACITY
    assert excinfo.value.http_status == 429
    assert "10 of 10" in excinfo.value.message
    assert "another host" in excinfo.value.message

    pressured = {
        "accepting": False,
        "reason": "memory_pressure",
        "active": 3,
        "limit": 10,
        "observed_at": time.time(),
    }
    with pytest.raises(OmnigentError) as excinfo:
        refuse_if_at_capacity(host_registry=_Registry(pressured), host_id="h")  # type: ignore[arg-type]
    assert "memory" in excinfo.value.message.lower()


def test_error_code_maps_to_429_exactly() -> None:
    """The wire code, the API code, and the HTTP status must agree."""
    from omnigent.host.frames import HOST_AT_CAPACITY_ERROR_CODE

    assert ErrorCode.HOST_AT_CAPACITY == HOST_AT_CAPACITY_ERROR_CODE
    assert OmnigentError("x", code=ErrorCode.HOST_AT_CAPACITY).http_status == 429
    # Neighbouring refusals keep their own statuses.
    assert OmnigentError("x", code=ErrorCode.HARNESS_NOT_CONFIGURED).http_status == 412
    assert OmnigentError("x", code=ErrorCode.CONFLICT).http_status == 409


def test_future_timestamps_beyond_skew_are_unknown() -> None:
    """A host stamped in the future is not evidence about the present.

    An absolute-difference freshness window let a badly-skewed host publish a
    refusal that stayed \"fresh\" for a full extra minute, blocking launches on
    a host that was actually accepting.
    """
    inside = 100.0 + CAPACITY_SNAPSHOT_MAX_SKEW_S
    assert capacity_snapshot_is_fresh({"observed_at": inside}, now=100.0) is True
    beyond = inside + 1
    assert capacity_snapshot_is_fresh({"observed_at": beyond}, now=100.0) is False
    # ...and such a snapshot cannot refuse a launch.
    refuse_if_at_capacity(
        host_registry=_Registry(
            {"accepting": False, "reason": "host_at_capacity", "observed_at": beyond}
        ),  # type: ignore[arg-type]
        host_id="h",
    )
