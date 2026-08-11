"""Authorization, idempotency, fencing, and redaction tests for CoDA lifecycle APIs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.onboarding.sandboxes.coda import CodaAppBinding, CodaProvider
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.managed_hosts import ManagedSandboxConfig
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio
ALICE = "alice@example.com"
BOB = "bob@example.com"
APP_ID = "public-selector-a"
HOST_ID = "c0da058a4a48002a654a80be0ee09bfb"
LEASE = "opaque-lease-secret"


class LifecycleHarness:
    def __init__(self, db_uri: str, tmp_path: Path) -> None:
        self.conversations = SqlAlchemyConversationStore(db_uri)
        self.permissions = SqlAlchemyPermissionStore(db_uri)
        self.hosts = HostStore(db_uri)
        self.calls: list[tuple[str, str, object]] = []
        self.fail_disconnect = False

        def request(method: str, path: str, body: object) -> dict[str, object]:
            self.calls.append((method, path, body))
            if self.fail_disconnect and path.endswith("/disconnect"):
                raise click.ClickException(
                    "upstream https://private-app.example token=do-not-leak owner=alice"
                )
            return {"ready": True}

        self.provider = CodaProvider(
            apps=(CodaAppBinding(APP_ID, "private-app-name", "https://private-app.example"),),
            request_fns={APP_ID: request},
            app_getter=lambda _name: SimpleNamespace(
                compute_status=SimpleNamespace(state="ACTIVE")
            ),
        )
        config = ManagedSandboxConfig(
            server_url="https://server.invalid",
            launcher_factory=lambda: self.provider,
            token_ttl_s=3600,
            provider="coda",
            max_sessions_per_lease=10,
        )
        artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
        agents = SqlAlchemyAgentStore(db_uri)
        self.app: FastAPI = create_app(
            agent_store=agents,
            file_store=SqlAlchemyFileStore(db_uri),
            conversation_store=self.conversations,
            artifact_store=artifacts,
            agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
            permission_store=self.permissions,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            host_store=self.hosts,
            sandbox_config=config,
        )

    def add_claim(self, *, owner: str = ALICE, sessions: int = 2) -> list[str]:
        self.hosts.register_managed_host(
            host_id=HOST_ID,
            name="sensitive-host-name",
            user_id=owner,
            token="host-token-secret",
            provider="coda",
            sandbox_id=f"coda:{APP_ID}#{LEASE}",
            token_expires_at=4_000_000_000,
        )
        ids: list[str] = []
        for index in range(sessions):
            conv = self.conversations.create_conversation(title=f"retained-{index}")
            self.conversations.set_labels(conv.id, {"retained": str(index)})
            self.conversations.set_host_id(
                conv.id,
                HOST_ID,
                workspace=f"/tmp/destroyed-{index}",
            )
            self.permissions.ensure_user(owner)
            self.permissions.grant(owner, conv.id, LEVEL_OWNER)
            ids.append(conv.id)
        return ids

    @property
    def disconnects(self) -> list[tuple[str, str, object]]:
        return [call for call in self.calls if call[1].endswith("/disconnect")]


@pytest.fixture()
def lifecycle(db_uri: str, tmp_path: Path) -> LifecycleHarness:
    return LifecycleHarness(db_uri, tmp_path)


@pytest_asyncio.fixture()
async def lifecycle_client(lifecycle: LifecycleHarness) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=lifecycle.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _headers(user: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


async def test_lifecycle_requires_authentication(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    assert (await lifecycle_client.get("/v1/coda/claims")).status_code == 401
    assert (await lifecycle_client.post(f"/v1/sessions/{session_id}/release")).status_code == 401
    assert (
        await lifecycle_client.post(f"/v1/sessions/{session_id}/resume", json={})
    ).status_code == 401


async def test_cross_user_release_is_concealed_and_non_mutating(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    response = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/release", headers=_headers(BOB)
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"
    assert lifecycle.conversations.get_conversation(session_id).host_id == HOST_ID  # type: ignore[union-attr]
    assert lifecycle.disconnects == []


async def test_session_release_preserves_history_and_shared_claim_then_is_idempotent(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    first, second = lifecycle.add_claim()
    response = await lifecycle_client.post(
        f"/v1/sessions/{first}/release", headers=_headers(ALICE)
    )
    assert response.status_code == 204
    detached = lifecycle.conversations.get_conversation(first)
    sibling = lifecycle.conversations.get_conversation(second)
    assert detached is not None and detached.detached_at is not None
    assert detached.title == "retained-0" and detached.labels == {"retained": "0"}
    assert detached.host_id is None and detached.workspace is None
    assert sibling is not None and sibling.host_id == HOST_ID
    assert lifecycle.hosts.get_host(HOST_ID) is not None
    assert lifecycle.disconnects == []

    repeated = await lifecycle_client.post(
        f"/v1/sessions/{first}/release", headers=_headers(ALICE)
    )
    assert repeated.status_code == 204
    assert lifecycle.disconnects == []

    final = await lifecycle_client.post(f"/v1/sessions/{second}/release", headers=_headers(ALICE))
    assert final.status_code == 204
    assert len(lifecycle.disconnects) == 1
    assert lifecycle.disconnects[0][2] == {"lease_id": LEASE, "scrub": True}
    assert lifecycle.hosts.get_host(HOST_ID) is None


async def test_concurrent_releases_serialize_and_disconnect_once(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    first, second = await asyncio.gather(
        lifecycle_client.post(f"/v1/sessions/{session_id}/release", headers=_headers(ALICE)),
        lifecycle_client.post(f"/v1/sessions/{session_id}/release", headers=_headers(ALICE)),
    )
    assert {first.status_code, second.status_code} == {204}
    assert len(lifecycle.disconnects) == 1
    assert lifecycle.hosts.get_host(HOST_ID) is None


async def test_sandbox_release_detaches_all_and_scrubs_once(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    sessions = lifecycle.add_claim()
    response = await lifecycle_client.post(
        f"/v1/coda/claims/{sessions[0]}/release", headers=_headers(ALICE)
    )
    assert response.status_code == 204
    assert len(lifecycle.disconnects) == 1
    for session_id in sessions:
        conv = lifecycle.conversations.get_conversation(session_id)
        assert conv is not None and conv.detached_at is not None and conv.host_id is None
        assert conv.title is not None
    assert lifecycle.hosts.get_host(HOST_ID) is None


async def test_ambiguous_disconnect_preserves_fence_and_redacts_error(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    lifecycle.fail_disconnect = True
    response = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/release", headers=_headers(ALICE)
    )
    assert response.status_code == 503
    serialized = response.text
    for secret in (APP_ID, LEASE, "private-app", "alice", "token"):
        assert secret not in serialized
    host = lifecycle.hosts.get_host(HOST_ID)
    assert host is not None and host.sandbox_id == f"coda:{APP_ID}#{LEASE}"
    conv = lifecycle.conversations.get_conversation(session_id)
    assert conv is not None and conv.detached_at is not None and conv.host_id is None
    assert conv.detached_claim_host_id == HOST_ID

    lifecycle.fail_disconnect = False
    retry = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/release", headers=_headers(ALICE)
    )
    assert retry.status_code == 204
    assert lifecycle.hosts.get_host(HOST_ID) is None
    recovered = lifecycle.conversations.get_conversation(session_id)
    assert recovered is not None and recovered.detached_claim_host_id is None


async def test_inventory_is_owner_scoped_and_sanitized(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    sessions = lifecycle.add_claim()
    response = await lifecycle_client.get("/v1/coda/claims", headers=_headers(ALICE))
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["claims"][0]["anchor_session_id"] in sessions
    assert {entry["id"] for entry in body["claims"][0]["sessions"]} == set(sessions)
    serialized = response.text
    for secret in (APP_ID, LEASE, HOST_ID, "private-app", "host-token", ALICE):
        assert secret not in serialized
    other = await lifecycle_client.get("/v1/coda/claims", headers=_headers(BOB))
    assert other.status_code == 200 and other.json() == {"claims": []}


@pytest.mark.parametrize(
    ("resume_json", "expected_app_id"),
    [({}, None), ({"sandbox_app_id": APP_ID}, APP_ID)],
)
async def test_resume_automatic_or_manual_binds_fresh_workspace_idempotently(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
    monkeypatch: pytest.MonkeyPatch,
    resume_json: dict[str, str],
    expected_app_id: str | None,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    lifecycle.conversations.set_labels(
        session_id,
        {"omnigent.sandbox.repo": "https://github.com/example/repo.git#main"},
    )
    released = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/release", headers=_headers(ALICE)
    )
    assert released.status_code == 204
    calls: list[str | None] = []
    reconstructed: list[tuple[str, str | None]] = []

    async def fake_launch(**kwargs: Any) -> None:
        calls.append(kwargs["coda_app_id"])
        repo = kwargs["repo"]
        reconstructed.append((repo.url, repo.branch))
        new_host_id = "e0da058a4a48002a654a80be0ee09bfb"
        lifecycle.hosts.register_managed_host(
            host_id=new_host_id,
            name="new-sensitive-host",
            user_id=ALICE,
            token="new-secret",
            provider="coda",
            sandbox_id=f"coda:{APP_ID}#new-opaque-lease",
            token_expires_at=4_000_000_000,
        )
        lifecycle.conversations.set_host_id(
            session_id, new_host_id, workspace="/tmp/fresh-reconstructed-workspace"
        )
        kwargs["tracker"].finish(session_id)

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._run_managed_launch",
        fake_launch,
    )
    resumed = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/resume",
        headers=_headers(ALICE),
        json=resume_json,
    )
    assert resumed.status_code == 204
    conv = lifecycle.conversations.get_conversation(session_id)
    assert conv is not None and conv.detached_at is None
    assert conv.workspace == "/tmp/fresh-reconstructed-workspace"
    assert conv.labels["omnigent.sandbox.repo"].endswith("#main")
    assert calls == [expected_app_id]
    assert reconstructed == [("https://github.com/example/repo.git", "main")]

    repeated = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/resume",
        headers=_headers(ALICE),
        json=resume_json,
    )
    assert repeated.status_code == 204
    assert calls == [expected_app_id]


async def test_resume_failure_leaves_session_detached_and_redacts_provider_error(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    assert (
        await lifecycle_client.post(f"/v1/sessions/{session_id}/release", headers=_headers(ALICE))
    ).status_code == 204

    async def fail_launch(**_kwargs: Any) -> None:
        raise click.ClickException(
            "https://private-app.example owner=alice token=secret upstream-body"
        )

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._run_managed_launch",
        fail_launch,
    )
    response = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/resume",
        headers=_headers(ALICE),
        json={"sandbox_app_id": APP_ID},
    )
    assert response.status_code == 503
    for secret in (APP_ID, "private-app", "alice", "token", "upstream-body"):
        assert secret not in response.text
    conv = lifecycle.conversations.get_conversation(session_id)
    assert conv is not None and conv.detached_at is not None
    assert conv.host_id is None and conv.workspace is None


async def test_removed_or_forged_manual_target_fails_before_mutation(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    assert (
        await lifecycle_client.post(f"/v1/sessions/{session_id}/release", headers=_headers(ALICE))
    ).status_code == 204
    response = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/resume",
        headers=_headers(ALICE),
        json={"sandbox_app_id": "removed-private-app"},
    )
    assert response.status_code == 409
    assert "removed-private-app" not in response.text
    conv = lifecycle.conversations.get_conversation(session_id)
    assert conv is not None and conv.detached_at is not None and conv.host_id is None


async def test_cross_user_resume_is_concealed(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    assert (
        await lifecycle_client.post(f"/v1/sessions/{session_id}/release", headers=_headers(ALICE))
    ).status_code == 204
    response = await lifecycle_client.post(
        f"/v1/sessions/{session_id}/resume", headers=_headers(BOB), json={}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"
    assert lifecycle.conversations.get_conversation(session_id).detached_at is not None  # type: ignore[union-attr]


async def test_concurrent_resume_acquires_once(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    assert (
        await lifecycle_client.post(f"/v1/sessions/{session_id}/release", headers=_headers(ALICE))
    ).status_code == 204
    launches = 0

    async def fake_launch(**kwargs: Any) -> None:
        nonlocal launches
        launches += 1
        await asyncio.sleep(0.02)
        new_host_id = "f0da058a4a48002a654a80be0ee09bfb"
        lifecycle.hosts.register_managed_host(
            host_id=new_host_id,
            name="concurrent-new-host",
            user_id=ALICE,
            token="new-secret",
            provider="coda",
            sandbox_id=f"coda:{APP_ID}#concurrent-lease",
            token_expires_at=4_000_000_000,
        )
        lifecycle.conversations.set_host_id(
            session_id, new_host_id, workspace="/tmp/concurrent-fresh"
        )
        kwargs["tracker"].finish(session_id)

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._run_managed_launch",
        fake_launch,
    )
    responses = await asyncio.gather(
        lifecycle_client.post(
            f"/v1/sessions/{session_id}/resume", headers=_headers(ALICE), json={}
        ),
        lifecycle_client.post(
            f"/v1/sessions/{session_id}/resume", headers=_headers(ALICE), json={}
        ),
    )
    assert [response.status_code for response in responses] == [204, 204]
    assert launches == 1


async def test_clients_cannot_supply_lease_or_host_to_resume(
    lifecycle_client: httpx.AsyncClient,
    lifecycle: LifecycleHarness,
) -> None:
    session_id = lifecycle.add_claim(sessions=1)[0]
    await lifecycle_client.post(f"/v1/sessions/{session_id}/release", headers=_headers(ALICE))
    for forged in ({"lease_id": LEASE}, {"host_id": HOST_ID}):
        response = await lifecycle_client.post(
            f"/v1/sessions/{session_id}/resume",
            headers=_headers(ALICE),
            json=forged,
        )
        assert response.status_code == 422
        assert lifecycle.conversations.get_conversation(session_id).host_id is None  # type: ignore[union-attr]
