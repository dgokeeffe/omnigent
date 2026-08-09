"""Ownership-checked resolution for host runner launches.

Two routes spawn a runner subprocess on a user's host machine and
bind it to a session: ``POST /v1/sessions`` (inline host launch) and
``POST /v1/hosts/{host_id}/runners``. A runner executes arbitrary
tools (shell, file I/O) on the host as that host's user, so a launch
must be authorized against BOTH the host and the session:

- the caller must own the target host (else they could run code on
  another user's machine — cross-user RCE), and
- the caller must own the target session (else they could bind their
  runner to another user's session, or another user's host to their
  session — cross-user hijack / data theft).

Centralizing the checks here keeps the two call sites from drifting
(the original bug was each site enforcing a different subset).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.permissions import check_session_access
from omnigent.stores import ConversationStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore

#: How recent a capacity snapshot must be to justify refusing a launch
#: before it reaches the host. Hosts publish on every transition plus a
#: periodic refresh, so anything older than this is treated as unknown and
#: the launch proceeds to the host's authoritative gate.
CAPACITY_SNAPSHOT_MAX_AGE_S = 60.0

#: Tolerated clock skew between a host and this replica. A snapshot stamped
#: further in the future than this is not evidence about the present, so it is
#: treated as unknown rather than as a fresh refusal.
CAPACITY_SNAPSHOT_MAX_SKEW_S = 5.0


def capacity_snapshot_is_fresh(
    snapshot: dict[str, object] | None,
    *,
    now: float | None = None,
) -> bool:
    """Whether an advisory capacity snapshot is recent enough to act on.

    :param snapshot: Serialized snapshot from ``HostRegistry`` or ``None``.
    :param now: Injectable epoch seconds for tests.
    :returns: ``True`` only when the snapshot exists and carries an
        ``observed_at`` that is at most
        :data:`CAPACITY_SNAPSHOT_MAX_AGE_S` old and no more than
        :data:`CAPACITY_SNAPSHOT_MAX_SKEW_S` in the future. A missing or
        unparseable timestamp is treated as unknown.
    """
    if not snapshot:
        return False
    observed_at = snapshot.get("observed_at")
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
        return False
    age = (now if now is not None else time.time()) - observed_at
    return -CAPACITY_SNAPSHOT_MAX_SKEW_S <= age <= CAPACITY_SNAPSHOT_MAX_AGE_S


def refuse_if_at_capacity(
    *,
    host_registry: HostRegistry,
    host_id: str,
) -> None:
    """Refuse early when a fresh snapshot says the host stopped accepting.

    A cheap server-side preflight so a doomed launch does not first create
    a git worktree and mint a binding token. It is deliberately advisory:
    an unknown or stale snapshot lets the launch through, and the host's
    own admission check remains the authority (it also covers the race
    where capacity frees up or fills between snapshot and launch).

    :param host_registry: In-memory live host connections (this replica).
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :raises OmnigentError: ``HOST_AT_CAPACITY`` (429) when a fresh
        snapshot reports the host is not accepting new runners.
    """
    snapshot = host_registry.capacity_snapshot(host_id)
    if not capacity_snapshot_is_fresh(snapshot):
        return
    assert snapshot is not None  # narrowed by capacity_snapshot_is_fresh
    if snapshot.get("accepting") is not False:
        return
    if snapshot.get("reason") == "memory_pressure":
        raise OmnigentError(
            "This host paused new runners because its memory is above the "
            "safe launch watermark. Wait for a session to finish, close one, "
            "or use another host.",
            code=ErrorCode.HOST_AT_CAPACITY,
        )
    limit = snapshot.get("limit")
    active = snapshot.get("active")
    detail = (
        f" ({active} of {limit} runners in use)"
        if isinstance(limit, int) and isinstance(active, int)
        else ""
    )
    raise OmnigentError(
        f"This host is at capacity{detail}. Wait for a session to finish, "
        "stop one, or use another host.",
        code=ErrorCode.HOST_AT_CAPACITY,
    )


@dataclass
class HostLaunchTarget:
    """A host + session pair the caller is authorized to launch on.

    :param host: The persistent host record (owned by the caller).
    :param conn: The live host WebSocket connection on this replica,
        used to send the launch frame.
    :param conv: The session/conversation the runner will bind to.
    """

    host: Host
    conn: HostConnection
    conv: Conversation


def resolve_host_owner(
    *,
    user_id: str | None,
    host_id: str,
    host_store: HostStore,
) -> Host:
    """
    Authorize that the caller owns a known host.

    Every route that reaches a host on the caller's behalf must pass
    this first so the owner check can't drift between them: the runner
    launch (via :func:`resolve_host_launch`) AND the session-create
    workspace probe, which sends a ``host.stat`` to the host. The
    original bug had that probe contacting another user's host before
    any ownership check. When ``user_id`` is ``None`` (auth disabled)
    the check is skipped, consistent with single-user/local behavior.

    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param host_store: Persistent host registrations.
    :returns: The host record owned by the caller.
    :raises HTTPException: 404 if the host is unknown; 403 if it is
        owned by a different user.
    """
    host = host_store.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    if user_id is not None and host.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your host")
    return host


def resolve_host_launch(
    *,
    user_id: str | None,
    host_id: str,
    session_id: str,
    host_store: HostStore,
    host_registry: HostRegistry,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
) -> HostLaunchTarget:
    """
    Resolve and authorize a host runner launch.

    Verifies the host exists, is owned by the caller, and is online,
    and that the caller owns the target session, before any runner is
    spawned. When ``user_id`` is ``None`` (auth disabled) the host-owner
    check is skipped; when ``permission_store`` is ``None`` (auth
    disabled) the session-owner check is skipped — both consistent with
    the single-user/local deployment behavior elsewhere.

    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param session_id: Session to bind the runner to, e.g.
        ``"conv_abc123"``.
    :param host_store: Persistent host registrations.
    :param host_registry: In-memory live host connections (this replica).
    :param conversation_store: Conversation lookups (also used by the
        session-access check for sub-agent parent delegation).
    :param permission_store: Session permission store, or ``None`` to
        skip the session-owner check (auth disabled).
    :returns: A :class:`HostLaunchTarget` with the validated host,
        connection, and conversation.
    :raises HTTPException: 404 if the host or session is missing (or the
        session is not owned by the caller — 404, not 403, so other
        users' sessions aren't enumerable); 403 if the host is owned by
        a different user; 409 if the host is offline.
    """
    host = resolve_host_owner(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
    )

    conn = host_registry.get(host_id)
    if conn is None:
        raise HTTPException(status_code=409, detail="host is offline")

    conv = conversation_store.get_conversation(session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="session not found")

    # A runner executes tools as the session's driver, so only the
    # session owner may bind one. A non-owner has no owner-level grant
    # and is rejected. 404 (not 403) avoids leaking the existence of
    # other users' sessions.
    if permission_store is not None and not check_session_access(
        user_id,
        session_id,
        LEVEL_OWNER,
        permission_store,
        conversation_store,
    ):
        raise HTTPException(status_code=404, detail="session not found")

    return HostLaunchTarget(host=host, conn=conn, conv=conv)
