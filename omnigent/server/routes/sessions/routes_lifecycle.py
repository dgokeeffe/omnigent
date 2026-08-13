"""History-preserving managed CoDA release and resume routes."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities import Conversation
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.coda_owner_locks import (
    BlockingCallCompletedAfterCancellation,
    CodaOwnerLockRegistry,
    complete_cancellation_cleanup,
    get_coda_owner_locks,
    run_awaitable_cancellation_safe,
    run_blocking_cancellation_safe,
)
from omnigent.server.managed_hosts import MANAGED_REPO_LABEL_KEY
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


class CodaClaimSession(BaseModel):
    """A retained session affected by a claim-level release."""

    id: str
    title: str | None = None
    detached: bool = False


class CodaClaim(BaseModel):
    """Sanitized claim inventory; provider and lease identities are omitted."""

    anchor_session_id: str
    sessions: list[CodaClaimSession]


class CodaClaimInventory(BaseModel):
    claims: list[CodaClaim] = Field(default_factory=list)


class ResumeSessionRequest(BaseModel):
    """Resume target. A lease or host identifier is intentionally not accepted."""

    model_config = ConfigDict(extra="forbid")
    sandbox_app_id: str | None = Field(default=None, min_length=1, max_length=256)


def _owned_session(
    session_id: str,
    user_id: str | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
) -> Conversation:
    """Resolve owner access without disclosing whether a foreign session exists."""
    conv = conversation_store.get_conversation(session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if permission_store is not None:
        if user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        grant = permission_store.get(user_id, session_id)
        if grant is None or grant.level < LEVEL_OWNER:
            raise HTTPException(status_code=404, detail="Session not found")
    return conv


def _claim_lock(request: Request, owner: str) -> CodaOwnerLockRegistry:
    """Use the owner's create/adopt lock across claim lifecycle operations."""
    return get_coda_owner_locks(request.app.state)


def _host_is_owned(host: Any, owner: str) -> bool:
    return host is not None and host.user_id == owner and host.sandbox_provider == "coda"


def _raise_release_cancellation(cancellation_pending: bool) -> None:
    if cancellation_pending:
        raise asyncio.CancelledError


def register_lifecycle_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Register authenticated CoDA claim, release, and resume operations."""

    @router.get("/coda/claims", response_model=CodaClaimInventory)
    async def list_coda_claims(request: Request) -> CodaClaimInventory:
        user_id = require_user(request, auth_provider)
        owner = user_id if user_id is not None else RESERVED_USER_LOCAL
        host_store = getattr(request.app.state, "host_store", None)
        if host_store is None:
            return CodaClaimInventory()
        hosts = await asyncio.to_thread(host_store.list_hosts, owner)
        claims: list[CodaClaim] = []
        for host in hosts:
            if not _host_is_owned(host, owner) or host.sandbox_id is None:
                continue
            sessions = await asyncio.to_thread(
                conversation_store.list_conversations_by_host_id, host.host_id
            )
            detached_sessions = await asyncio.to_thread(
                conversation_store.list_conversations_by_detached_claim_host_id,
                host.host_id,
            )
            by_id = {session.id: session for session in [*sessions, *detached_sessions]}
            owned = [
                session
                for session in by_id.values()
                if permission_store is None
                or (
                    user_id is not None
                    and (grant := permission_store.get(user_id, session.id)) is not None
                    and grant.level >= LEVEL_OWNER
                )
            ]
            if not owned:
                continue
            public_sessions = [
                CodaClaimSession(
                    id=session.id,
                    title=session.title,
                    detached=session.detached_at is not None,
                )
                for session in sorted(owned, key=lambda item: (item.created_at, item.id))
            ]
            claims.append(
                CodaClaim(
                    anchor_session_id=public_sessions[0].id,
                    sessions=public_sessions,
                )
            )
        return CodaClaimInventory(claims=claims)

    @router.post("/sessions/{session_id}/release", status_code=204, response_model=None)
    async def release_session(request: Request, session_id: str) -> Response:
        user_id = require_user(request, auth_provider)
        owner = user_id if user_id is not None else RESERVED_USER_LOCAL
        conv = await asyncio.to_thread(
            _owned_session,
            session_id,
            user_id,
            conversation_store,
            permission_store,
        )
        if conv.kind == "sub_agent":
            raise HTTPException(status_code=409, detail="Release the root session or sandbox")
        if conv.live_status in ("running", "waiting"):
            raise HTTPException(status_code=409, detail="Session is busy; stop it and retry")

        cancellation_pending = False
        async with _claim_lock(request, owner).hold(owner):
            conv = await asyncio.to_thread(
                _owned_session,
                session_id,
                user_id,
                conversation_store,
                permission_store,
            )
            if conv.live_status in ("running", "waiting"):
                raise HTTPException(status_code=409, detail="Session is busy; stop it and retry")
            if (
                conv.detached_at is not None
                and conv.host_id is None
                and conv.detached_claim_host_id is None
            ):
                return Response(status_code=204)

            # A first CoDA create returns while this owner's launch task is
            # provisioning. Release must wait for that matching task while it
            # still owns the same owner lock; otherwise detaching the unbound
            # row races set_host_id(), which would silently reattach it later.
            if conv.host_id is None and conv.detached_claim_host_id is None:
                owner_launches = getattr(request.app.state, "coda_owner_launches", {})
                launch_sessions = getattr(request.app.state, "coda_owner_launch_sessions", {})
                launch_task = owner_launches.get(owner)
                if launch_task is not None and launch_sessions.get(owner) == session_id:
                    try:
                        await run_awaitable_cancellation_safe(launch_task)
                    except BlockingCallCompletedAfterCancellation:
                        cancellation_pending = True
                    except asyncio.CancelledError:
                        # The background task itself was cancelled (for example,
                        # during shutdown); this request still owns the row and
                        # can safely detach it after re-reading below.
                        pass
                    except Exception:
                        # Launch failures are already recorded by the tracker.
                        # Release remains a cleanup operation and exposes none
                        # of the provider exception.
                        pass
                    conv = await asyncio.to_thread(
                        _owned_session,
                        session_id,
                        user_id,
                        conversation_store,
                        permission_store,
                    )

            if conv.host_id is None and conv.detached_claim_host_id is None:
                try:
                    await run_blocking_cancellation_safe(
                        conversation_store.detach_conversation, session_id
                    )
                except BlockingCallCompletedAfterCancellation:
                    cancellation_pending = True
                _raise_release_cancellation(cancellation_pending)
                return Response(status_code=204)

            claim_host_id = conv.host_id or conv.detached_claim_host_id
            host_store = getattr(request.app.state, "host_store", None)
            if host_store is None:
                raise HTTPException(status_code=409, detail="Sandbox claim is unavailable")
            host = (
                await asyncio.to_thread(host_store.get_host, claim_host_id)
                if claim_host_id is not None
                else None
            )
            if host is None or not _host_is_owned(host, owner) or host.sandbox_id is None:
                raise HTTPException(status_code=409, detail="Sandbox claim is unavailable")
            siblings = await asyncio.to_thread(
                conversation_store.list_conversations_by_host_id, host.host_id
            )
            has_siblings = any(item.id != session_id for item in siblings)
            has_repository = MANAGED_REPO_LABEL_KEY in conv.labels
            if has_siblings and has_repository:
                # The shared claim remains live, so release this repository
                # allocation before detaching it. Resume can then reconstruct a
                # fresh checkout at the same durable session id without a
                # partial-directory conflict. Non-repository Release retains
                # its protocol-v1 behavior and needs no workspace cleanup.
                from omnigent.onboarding.sandboxes.coda import CodaProvider

                config = getattr(request.app.state, "sandbox_config", None)
                launcher = config.launcher_factory() if config is not None else None
                if not isinstance(launcher, CodaProvider):
                    raise HTTPException(status_code=409, detail="Sandbox claim is unavailable")
                try:
                    await run_blocking_cancellation_safe(
                        launcher.release_workspace,
                        host.sandbox_id,
                        session_id,
                    )
                except BlockingCallCompletedAfterCancellation:
                    cancellation_pending = True
                except Exception as exc:
                    raise HTTPException(
                        status_code=503,
                        detail="Session workspace release failed; retry Release",
                    ) from exc
            try:
                await run_blocking_cancellation_safe(
                    conversation_store.detach_conversation,
                    session_id,
                    expected_host_id=host.host_id,
                )
            except BlockingCallCompletedAfterCancellation:
                cancellation_pending = True
            if has_siblings:
                try:
                    await run_blocking_cancellation_safe(
                        conversation_store.clear_detached_claim_host_id, host.host_id
                    )
                except BlockingCallCompletedAfterCancellation:
                    cancellation_pending = True
                _raise_release_cancellation(cancellation_pending)
                return Response(status_code=204)
            from omnigent.server.managed_hosts import terminate_managed_host

            try:
                await run_awaitable_cancellation_safe(
                    terminate_managed_host(
                        host,
                        host_store,
                        getattr(request.app.state, "sandbox_config", None),
                    )
                )
            except BlockingCallCompletedAfterCancellation:
                cancellation_pending = True
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Sandbox release is uncertain; retry this release",
                ) from exc
            try:
                await run_blocking_cancellation_safe(
                    conversation_store.clear_detached_claim_host_id, host.host_id
                )
            except BlockingCallCompletedAfterCancellation:
                cancellation_pending = True
            _raise_release_cancellation(cancellation_pending)
        return Response(status_code=204)

    @router.post("/coda/claims/{session_id}/release", status_code=204, response_model=None)
    async def release_sandbox(request: Request, session_id: str) -> Response:
        user_id = require_user(request, auth_provider)
        owner = user_id if user_id is not None else RESERVED_USER_LOCAL
        anchor = await asyncio.to_thread(
            _owned_session,
            session_id,
            user_id,
            conversation_store,
            permission_store,
        )
        if (
            anchor.detached_at is not None
            and anchor.host_id is None
            and anchor.detached_claim_host_id is None
        ):
            return Response(status_code=204)
        if anchor.host_id is None and anchor.detached_claim_host_id is None:
            raise HTTPException(status_code=409, detail="Sandbox claim is unavailable")

        cancellation_pending = False
        async with _claim_lock(request, owner).hold(owner):
            anchor = await asyncio.to_thread(
                _owned_session,
                session_id,
                user_id,
                conversation_store,
                permission_store,
            )
            if (
                anchor.detached_at is not None
                and anchor.host_id is None
                and anchor.detached_claim_host_id is None
            ):
                return Response(status_code=204)
            claim_host_id = anchor.host_id or anchor.detached_claim_host_id
            host_store = getattr(request.app.state, "host_store", None)
            if host_store is None:
                raise HTTPException(status_code=409, detail="Sandbox claim is unavailable")
            host = (
                await asyncio.to_thread(host_store.get_host, claim_host_id)
                if claim_host_id is not None
                else None
            )
            if host is None or not _host_is_owned(host, owner) or host.sandbox_id is None:
                raise HTTPException(status_code=409, detail="Sandbox claim is unavailable")
            sessions = await asyncio.to_thread(
                conversation_store.list_conversations_by_host_id, host.host_id
            )
            for session in sessions:
                await asyncio.to_thread(
                    _owned_session,
                    session.id,
                    user_id,
                    conversation_store,
                    permission_store,
                )
                if session.live_status in ("running", "waiting"):
                    raise HTTPException(
                        status_code=409,
                        detail="A sandbox session is busy; stop it and retry",
                    )
            await asyncio.to_thread(
                conversation_store.detach_conversations_by_host_id, host.host_id
            )
            from omnigent.server.managed_hosts import terminate_managed_host

            try:
                await run_awaitable_cancellation_safe(
                    terminate_managed_host(
                        host,
                        host_store,
                        getattr(request.app.state, "sandbox_config", None),
                    )
                )
            except BlockingCallCompletedAfterCancellation:
                cancellation_pending = True
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Sandbox release is uncertain; retry this release",
                ) from exc
            try:
                await run_blocking_cancellation_safe(
                    conversation_store.clear_detached_claim_host_id, host.host_id
                )
            except BlockingCallCompletedAfterCancellation:
                cancellation_pending = True
            _raise_release_cancellation(cancellation_pending)
        return Response(status_code=204)

    @router.post("/sessions/{session_id}/resume", status_code=204, response_model=None)
    async def resume_session(
        request: Request,
        session_id: str,
        body: ResumeSessionRequest | None = None,
    ) -> Response:
        user_id = require_user(request, auth_provider)
        owner = user_id if user_id is not None else RESERVED_USER_LOCAL
        conv = await asyncio.to_thread(
            _owned_session,
            session_id,
            user_id,
            conversation_store,
            permission_store,
        )
        if conv.kind == "sub_agent":
            raise HTTPException(status_code=409, detail="Resume the root session")
        if conv.host_id is not None and conv.detached_at is None:
            return Response(status_code=204)
        if conv.detached_at is None:
            raise HTTPException(status_code=409, detail="Session is not detached")
        if conv.detached_claim_host_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Prior sandbox release is unresolved; retry Release before Resume",
            )

        config = getattr(request.app.state, "sandbox_config", None)
        host_store = getattr(request.app.state, "host_store", None)
        tracker = getattr(request.app.state, "managed_launches", None)
        if config is None or host_store is None or tracker is None or config.provider != "coda":
            raise HTTPException(status_code=409, detail="CoDA sandbox resume is unavailable")
        app_id = body.sandbox_app_id if body is not None else None

        async with _claim_lock(request, owner).hold(owner):
            conv = await asyncio.to_thread(
                _owned_session,
                session_id,
                user_id,
                conversation_store,
                permission_store,
            )
            if conv.host_id is not None and conv.detached_at is None:
                return Response(status_code=204)
            if conv.detached_at is None:
                raise HTTPException(status_code=409, detail="Session is not detached")
            if conv.detached_claim_host_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Prior sandbox release is unresolved; retry Release before Resume",
                )

            from omnigent.onboarding.sandboxes.coda import CodaProvider
            from omnigent.server.managed_hosts import (
                MANAGED_REPO_LABEL_KEY,
                ManagedHostLaunch,
                parse_repo_workspace,
                terminate_managed_host,
            )
            from omnigent.server.routes._sessions.orchestration import (
                _bind_and_launch_managed_runner,
                _run_managed_launch,
            )
            from omnigent.stores.host_store import host_is_live

            if app_id is not None:
                try:
                    target_launcher = config.launcher_factory()
                    if not isinstance(target_launcher, CodaProvider):
                        raise RuntimeError("CoDA provider is unavailable")
                    await asyncio.to_thread(target_launcher.prepare, app_id)
                except Exception as exc:
                    raise HTTPException(
                        status_code=409,
                        detail="Selected sandbox is full, removed, or unavailable",
                    ) from exc

            hosts = await asyncio.to_thread(host_store.list_hosts, owner)
            candidates = [
                host
                for host in hosts
                if _host_is_owned(host, owner)
                and host.sandbox_id is not None
                and host_is_live(host)
                and (app_id is None or host.sandbox_id.startswith(f"coda:{app_id}#"))
            ]
            cap = config.max_sessions_per_lease or 10
            adopted = None
            for host in candidates:
                bound = await asyncio.to_thread(
                    conversation_store.list_conversations_by_host_id, host.host_id
                )
                if len(bound) < cap:
                    adopted = host
                    break

            raw_repo = conv.labels.get(MANAGED_REPO_LABEL_KEY)
            try:
                repo = parse_repo_workspace(raw_repo) if raw_repo else None
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="The retained repository workspace is invalid; Resume was not started",
                ) from exc

            tracker.begin(session_id)
            launch_state = tracker.get(session_id)
            new_host_id: str | None = adopted.host_id if adopted is not None else None
            workspace_allocated = False
            launcher: CodaProvider | None = None

            async def _cleanup_failed_resume() -> None:
                tracker.fail(session_id, "resume failed")
                if adopted is not None and workspace_allocated and launcher is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            launcher.release_workspace,
                            adopted.sandbox_id,
                            session_id,
                        )
                rebound = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if (
                    rebound is not None
                    and new_host_id is not None
                    and rebound.host_id == new_host_id
                ):
                    with contextlib.suppress(ValueError):
                        await asyncio.to_thread(
                            conversation_store.detach_conversation,
                            session_id,
                            expected_host_id=new_host_id,
                        )
                    # A newly acquired claim is ours to clean up. An adopted
                    # shared claim must remain for its sibling sessions.
                    if adopted is None:
                        failed_host = await asyncio.to_thread(host_store.get_host, new_host_id)
                        if failed_host is not None:
                            with contextlib.suppress(Exception):
                                await terminate_managed_host(failed_host, host_store, config)

            try:
                if adopted is not None:
                    candidate_launcher = config.launcher_factory()
                    launcher = (
                        candidate_launcher
                        if isinstance(candidate_launcher, CodaProvider)
                        else None
                    )
                    if launcher is None:
                        raise RuntimeError("CoDA provider is unavailable")
                    if repo is None:
                        workspace = await run_blocking_cancellation_safe(
                            launcher.allocate_workspace,
                            adopted.sandbox_id,
                            session_id,
                        )
                    else:
                        workspace = await run_blocking_cancellation_safe(
                            launcher.allocate_workspace,
                            adopted.sandbox_id,
                            session_id,
                            repo_url=repo.url,
                            repo_branch=repo.branch,
                            repo_name=repo.repo_name,
                        )
                    workspace_allocated = True
                    await _bind_and_launch_managed_runner(
                        session_id=session_id,
                        managed=ManagedHostLaunch(adopted.host_id, workspace),
                        sandbox_config=config,
                        tracker=tracker,
                        conversation_store=conversation_store,
                        host_store=host_store,
                        host_registry=getattr(request.app.state, "host_registry", None),
                        tunnel_registry=getattr(request.app.state, "tunnel_registry", None),
                    )
                else:
                    before_ids = {host.host_id for host in hosts}
                    await _run_managed_launch(
                        session_id=session_id,
                        owner=owner,
                        sandbox_config=config,
                        repo=repo,
                        tracker=tracker,
                        conversation_store=conversation_store,
                        host_store=host_store,
                        host_registry=getattr(request.app.state, "host_registry", None),
                        tunnel_registry=getattr(request.app.state, "tunnel_registry", None),
                        coda_app_id=app_id,
                        agent_store=agent_store,
                        agent_id=conv.agent_id,
                    )
                    rebound = await asyncio.to_thread(
                        conversation_store.get_conversation, session_id
                    )
                    if rebound is not None and rebound.host_id not in before_ids:
                        new_host_id = rebound.host_id
                if launch_state is not None and launch_state.error is not None:
                    raise RuntimeError("resume failed")
                rebound = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if rebound is None or rebound.host_id is None or rebound.detached_at is not None:
                    raise RuntimeError("resume failed")
            except BlockingCallCompletedAfterCancellation as exc:
                workspace_allocated = True
                await complete_cancellation_cleanup(_cleanup_failed_resume())
                raise asyncio.CancelledError from exc
            except asyncio.CancelledError:
                await complete_cancellation_cleanup(_cleanup_failed_resume())
                raise
            except Exception as exc:
                await _cleanup_failed_resume()
                raise HTTPException(
                    status_code=503,
                    detail="Resume failed; the session remains detached and can be retried",
                ) from exc
        return Response(status_code=204)
