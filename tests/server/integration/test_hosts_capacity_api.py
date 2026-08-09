"""Server-side capacity: API projection, preflight, and the typed 429.

Capacity travels host → server as an advisory snapshot (hello +
``host.capacity_update``) and is surfaced on host list/detail. A refusal at
launch time is authoritative and must become a stable ``host_at_capacity``
429 with the host's actionable message, after the same rollback the other
structured refusals perform.
"""

from __future__ import annotations

import asyncio

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostCapacitySnapshot,
    HostCapacityUpdateFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "33296f9b15e02671c34e013dd711407e"


def _snapshot(**overrides: object) -> HostCapacitySnapshot:
    fields: dict[str, object] = {
        "active": 4,
        "pending": 0,
        "limit": 10,
        "available": 6,
        "accepting": True,
        "reason": None,
        "pressure": False,
        "memory_used": 2048,
        "memory_limit": 8192,
        "memory_percent": 25.0,
        "memory_high_threshold": 80.0,
        "memory_resume_threshold": 70.0,
        "reserve_mb": 768,
        "observed_at": 1000.0,
    }
    fields.update(overrides)
    return HostCapacitySnapshot(**fields)  # type: ignore[arg-type]


def _websocket_scope(path: str) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def _build_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(create_hosts_router(registry, host_store, conv_store), prefix="/v1")

    @app.exception_handler(OmnigentError)
    async def _handle(request: object, exc: OmnigentError) -> JSONResponse:
        """Mirror create_app's OmnigentError → JSON handler."""
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return app, registry, host_store, conv_store


@pytest.fixture()
def capacity_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    return _build_app(db_uri)


async def _connect_host(
    app: FastAPI,
    registry: HostRegistry,
    *,
    capacity: HostCapacitySnapshot | None = None,
) -> ApplicationCommunicator:
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(
                HostHelloFrame(
                    version="0.1.0-test",
                    frame_protocol_version=1,
                    name="test-laptop",
                    capacity=capacity,
                )
            ),
        },
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    return comm


# ── API projection ───────────────────────────────────────────


async def test_list_and_detail_expose_capacity(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A reporting host's advisory snapshot appears on list and detail."""
    app, registry, _hs, _cs = capacity_app
    _comm = await _connect_host(app, registry, capacity=_snapshot())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/v1/hosts")
        detail = await client.get(f"/v1/hosts/{_HOST_ID}")

    for body in (listed.json()["hosts"][0], detail.json()):
        capacity = body["capacity"]
        assert capacity["active"] == 4
        assert capacity["limit"] == 10
        assert capacity["available"] == 6
        assert capacity["accepting"] is True
        # Freshness must travel with the value so a client can distrust it.
        assert capacity["observed_at"] == 1000.0
        assert capacity["memory_high_threshold"] == 80.0


async def test_capacity_is_null_for_older_host(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """An older host that reports nothing must read as unknown, not full.

    Serializing zeros here would disable a perfectly healthy host in the
    picker after a partial upgrade.
    """
    app, registry, _hs, _cs = capacity_app
    _comm = await _connect_host(app, registry, capacity=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/v1/hosts")
        detail = await client.get(f"/v1/hosts/{_HOST_ID}")

    assert listed.json()["hosts"][0]["capacity"] is None
    assert detail.json()["capacity"] is None


async def test_capacity_update_frame_refreshes_the_projection(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """host.capacity_update is ingested without touching the hosts table."""
    app, registry, _hs, _cs = capacity_app
    comm = await _connect_host(app, registry, capacity=_snapshot())

    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(
                HostCapacityUpdateFrame(
                    capacity=_snapshot(
                        active=10,
                        available=0,
                        accepting=False,
                        reason="host_at_capacity",
                        observed_at=2000.0,
                    )
                )
            ),
        },
    )
    for _ in range(200):
        snapshot = registry.capacity_snapshot(_HOST_ID)
        if snapshot is not None and snapshot["observed_at"] == 2000.0:
            break
        await asyncio.sleep(0.01)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/v1/hosts/{_HOST_ID}")
    capacity = detail.json()["capacity"]
    assert capacity["accepting"] is False
    assert capacity["available"] == 0
    assert capacity["reason"] == "host_at_capacity"
    # Ingesting capacity must not knock the host offline.
    assert detail.json()["status"] == "online"


async def test_hostile_capacity_values_cannot_break_the_host_api(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A host cannot poison its own API response or drop its tunnel.

    A NaN or oversized number reaching the registry makes GET /v1/hosts
    return invalid JSON (or 500), and an OverflowError escaping the decoder
    kills the receive loop because it only handles ValueError.
    """
    app, registry, _hs, _cs = capacity_app
    comm = await _connect_host(app, registry, capacity=_snapshot())

    for raw in (
        '{"kind": "host.capacity_update", "capacity": {"active": 1, "pending": 0,'
        ' "accepting": true, "memory_percent": NaN, "observed_at": Infinity}}',
        '{"kind": "host.capacity_update", "capacity": {"active": 1, "pending": 0,'
        ' "accepting": true, "memory_percent": 1e400}}',
        '{"kind": "host.capacity_update", "capacity": {"active": 1, "pending": 0,'
        ' "accepting": true, "memory_used": ' + str(10**400) + "}}",
    ):
        await comm.send_input({"type": "websocket.receive", "text": raw})
    await asyncio.sleep(0.15)

    assert registry.get(_HOST_ID) is not None, "the tunnel must survive"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/v1/hosts")
        detail = await client.get(f"/v1/hosts/{_HOST_ID}")
    assert listed.status_code == 200
    assert detail.status_code == 200
    capacity = detail.json()["capacity"]
    assert capacity["memory_percent"] is None
    assert capacity["observed_at"] is None or isinstance(capacity["observed_at"], float)
    body = listed.text
    assert "NaN" not in body and "Infinity" not in body


async def test_malformed_capacity_update_does_not_drop_the_tunnel(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A bad capacity frame is logged and dropped; the host stays usable."""
    app, registry, _hs, _cs = capacity_app
    comm = await _connect_host(app, registry, capacity=_snapshot())

    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": '{"kind": "host.capacity_update", "capacity": {"active": "x"}}',
        },
    )
    await asyncio.sleep(0.05)
    assert registry.get(_HOST_ID) is not None
    snapshot = registry.capacity_snapshot(_HOST_ID)
    assert snapshot is not None and snapshot["active"] == 4, (
        "a rejected frame must leave the previous snapshot intact"
    )


# ── authoritative 429 ────────────────────────────────────────


async def test_launch_at_capacity_returns_typed_429_and_rolls_back(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A host refusal maps to 429 + host_at_capacity and unbinds the session.

    Left bound, the session would point at a full host with no runner and
    no way for the user to tell why.
    """
    app, registry, _hs, conv_store = capacity_app
    # Report accepting so the advisory preflight lets the request through and
    # the AUTHORITATIVE refusal is what produces the 429.
    comm = await _connect_host(app, registry, capacity=_snapshot())
    conv = conv_store.create_conversation(agent_id=None)
    refusal = (
        "This host is at capacity (10 running, 0 starting, limit 10). "
        "Wait for a session to finish, stop one, or use another host."
    )

    async def _refuse() -> None:
        for _ in range(20):
            output = await comm.receive_output(timeout=2.0)
            if output["type"] != "websocket.send":
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostLaunchRunnerFrame):
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostLaunchRunnerResultFrame(
                                request_id=frame.request_id,
                                status="failed",
                                error=refusal,
                                error_code="host_at_capacity",
                            )
                        ),
                    },
                )
                return
        raise AssertionError("Host never received launch frame")

    responder = asyncio.create_task(_refuse())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/tmp/test-workspace"},
        )
    await responder

    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["error"]["code"] == "host_at_capacity"
    # The host's own actionable wording is preserved verbatim.
    assert body["error"]["message"] == refusal

    updated = conv_store.get_conversation(conv.id)
    assert updated is not None
    assert updated.runner_id is None, "a refused launch must unbind runner_id"
    assert updated.host_id is None, "a refused launch must unbind host_id"


async def test_preflight_short_circuits_before_the_host_is_asked(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A fresh full snapshot refuses without sending a launch frame at all.

    This is the cheap path: no binding token, no worktree, no rollback.
    """
    import time

    app, registry, _hs, conv_store = capacity_app
    comm = await _connect_host(
        app,
        registry,
        capacity=_snapshot(
            active=10,
            available=0,
            accepting=False,
            reason="host_at_capacity",
            observed_at=time.time(),
        ),
    )
    conv = conv_store.create_conversation(agent_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/tmp/test-workspace"},
        )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "host_at_capacity"
    # No launch frame was ever sent, and nothing was bound.
    with pytest.raises(asyncio.TimeoutError):
        await comm.receive_output(timeout=0.2)
    updated = conv_store.get_conversation(conv.id)
    assert updated is not None and updated.runner_id is None


async def test_invalid_input_still_wins_over_a_capacity_refusal(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A bad branch name must still be 400, not masked by a transient 429.

    The advisory preflight runs before the expensive worktree creation but
    AFTER input validation; putting it first turned every malformed request on
    a busy host into a misleading \"at capacity\" answer.
    """
    import time

    app, registry, _hs, conv_store = capacity_app
    _comm = await _connect_host(
        app,
        registry,
        capacity=_snapshot(
            active=10,
            available=0,
            accepting=False,
            reason="host_at_capacity",
            observed_at=time.time(),
        ),
    )
    conv = conv_store.create_conversation(agent_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={
                "session_id": conv.id,
                "workspace": "/tmp/test-workspace",
                "git": {"branch_name": "bad branch name", "base_branch": "main"},
            },
        )
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"


async def test_offline_host_still_returns_409_not_429(
    capacity_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """Capacity handling must not disturb the existing offline contract."""
    app, _registry, host_store, conv_store = capacity_app
    host_store.upsert_on_connect(host_id=_HOST_ID, name="test-laptop", user_id="local")
    host_store.set_offline(_HOST_ID)
    conv = conv_store.create_conversation(agent_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/tmp/test-workspace"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "host is offline"
