"""Managed-host launcher for a pool of pre-provisioned CoDA Databricks Apps."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar
from urllib import error, request

import click

from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

CODA_WORKSPACE_PATH = "/app/python/source_code"
_SENSITIVE_ERROR_KEYS = (
    "token", "secret", "authorization", "credential", "password", "api_key", "access_key"
)
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodaAppBinding:
    """One immutable routing identity and its mutable Databricks App metadata."""

    app_id: str
    app_name: str
    app_url: str


class CodaCapacityError(click.ClickException):
    """The authoritative lease endpoint reported that this App is full."""


class CodaAmbiguousAcquireError(click.ClickException):
    """A lease request may have committed but no response was received."""


class CodaUnavailableError(click.ClickException):
    """A fresh readiness probe could not establish that this App is usable."""


class CodaPoolState:
    """Process-local, concurrency-safe round-robin cursor shared by launcher instances."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor = 0

    def reserve_start(self, size: int) -> int:
        with self._lock:
            start = self._cursor % size
            self._cursor = (self._cursor + 1) % size
            return start


def _safe_control_error_detail(raw: str) -> str:
    """Return a bounded CoDA error detail with credential fields redacted."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "upstream control request failed"

    def _redact(value: object) -> object:
        if isinstance(value, dict):
            return {
                str(key): (
                    "<redacted>"
                    if any(part in str(key).lower() for part in _SENSITIVE_ERROR_KEYS)
                    else _redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [_redact(item) for item in value]
        if isinstance(value, str) and any(
            marker in value.lower() for marker in _SENSITIVE_ERROR_KEYS
        ):
            return "<redacted>"
        return value

    return json.dumps(_redact(payload), separators=(",", ":"))[:1024]


def _parse_sandbox_id(sandbox_id: str) -> tuple[str, str]:
    """Return the immutable App id and lease id from a fenced CoDA sandbox id."""
    if not sandbox_id.startswith("coda:") or "#" not in sandbox_id:
        raise click.ClickException(f"invalid CoDA sandbox id: {sandbox_id!r}")
    app_id, lease_id = sandbox_id[5:].rsplit("#", 1)
    if not app_id or not lease_id:
        raise click.ClickException(f"invalid CoDA sandbox id: {sandbox_id!r}")
    return app_id, lease_id


_RequestFn = Callable[[str, str, Mapping[str, object] | None], Mapping[str, object]]


class CodaProvider(SandboxHostLauncher):
    """Acquire across a CoDA pool and route existing leases to their granting App."""

    provider: ClassVar[str] = "coda"

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=False,
            programmatic_terminate=True,
        )

    def __init__(
        self,
        *,
        apps: Sequence[CodaAppBinding] | None = None,
        app_name: str | None = None,
        app_url: str | None = None,
        workspace_path: str = CODA_WORKSPACE_PATH,
        request_fn: _RequestFn | None = None,
        request_fns: Mapping[str, _RequestFn] | None = None,
        app_getter: Callable[[str], object] | None = None,
        pool_state: CodaPoolState | None = None,
    ) -> None:
        if apps is None:
            if not app_name or not app_url:
                raise ValueError("CoDA requires apps or legacy app_name/app_url")
            apps = (CodaAppBinding(app_name, app_name, app_url),)
        normalized = tuple(
            CodaAppBinding(item.app_id, item.app_name, item.app_url.rstrip("/")) for item in apps
        )
        if not normalized:
            raise ValueError("CoDA pool must not be empty")
        self._apps = normalized
        self._registry = {item.app_id: item for item in normalized}
        if len(self._registry) != len(normalized):
            raise ValueError("CoDA app_id values must be unique")
        if request_fn is not None and len(normalized) != 1:
            raise ValueError("request_fn is only valid for a single CoDA App; use request_fns")
        self._request_fns = dict(request_fns or {})
        if request_fn is not None:
            self._request_fns[normalized[0].app_id] = request_fn
        self._workspace_path = workspace_path
        self._app_getter = app_getter or self._get_app
        self._pool_state = pool_state or CodaPoolState()
        self._lease_owner: str | None = None

    @property
    def app_ids(self) -> tuple[str, ...]:
        """Configured immutable App identities in deterministic pool order."""
        return tuple(item.app_id for item in self._apps)

    def set_lease_owner(self, owner: str) -> None:
        self._lease_owner = owner

    @staticmethod
    def _get_app(app_name: str) -> object:
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient().apps.get(app_name)

    def _request_for(
        self, binding: CodaAppBinding, method: str, path: str, body: Mapping[str, object] | None
    ) -> Mapping[str, object]:
        override = self._request_fns.get(binding.app_id)
        if override is not None:
            return override(method, path, body)
        from databricks.sdk.core import Config

        headers = dict(Config().authenticate())
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(
            f"{binding.app_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                payload = response.read()
        except error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            detail = _safe_control_error_detail(raw)
            if exc.code == 409 and path == "/api/omnigent-host/lease":
                try:
                    response_body = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    response_body = {}
                if response_body.get("error") != "app_name does not match this CoDA instance":
                    raise CodaCapacityError("CoDA app has no available lease capacity") from exc
            raise click.ClickException(
                f"CoDA control request failed ({exc.code}): {detail}"
            ) from exc
        except error.URLError as exc:
            if path == "/api/omnigent-host/lease":
                raise CodaAmbiguousAcquireError(
                    f"CoDA lease response was ambiguous: {exc.reason}"
                ) from exc
            raise CodaUnavailableError(f"CoDA control request failed: {exc.reason}") from exc
        try:
            decoded = json.loads(payload or b"{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise click.ClickException("CoDA control response must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise click.ClickException("CoDA control response must be a JSON object")
        return decoded

    def _probe(self, binding: CodaAppBinding) -> None:
        try:
            app = self._app_getter(binding.app_name)
        except Exception as exc:
            raise CodaUnavailableError(
                f"CoDA app {binding.app_id!r} status is unavailable"
            ) from exc
        compute = getattr(getattr(app, "compute_status", None), "state", None)
        if str(compute).upper().split(".")[-1] != "ACTIVE":
            raise CodaUnavailableError(
                f"CoDA app {binding.app_id!r} compute is not ACTIVE"
            )
        status = self._request_for(binding, "GET", "/api/omnigent-host/status", None)
        if status.get("ready") is not True:
            raise CodaUnavailableError(f"CoDA app {binding.app_id!r} is not ready")

    def prepare(self) -> None:
        """Require at least one currently ready App without mutating the pool."""
        failures: list[str] = []
        for binding in self._apps:
            try:
                self._probe(binding)
                return
            except CodaUnavailableError as exc:
                failures.append(f"{binding.app_id}: {exc.message}")
        raise click.ClickException("no ready CoDA Apps: " + "; ".join(failures))

    def provision(self, name: str) -> str:
        """Acquire a NEW lease with bounded fresh-probe and capacity spillover."""
        lease_id = uuid.uuid4().hex
        start = self._pool_state.reserve_start(len(self._apps))
        rejected: list[str] = []
        for offset in range(len(self._apps)):
            binding = self._apps[(start + offset) % len(self._apps)]
            try:
                self._probe(binding)
            except CodaUnavailableError:
                rejected.append(f"{binding.app_id}=unavailable")
                _logger.info(
                    "CoDA acquisition rejected app_id=%s category=unavailable",
                    binding.app_id,
                )
                continue
            body = {
                "action": "acquire",
                "app_name": binding.app_name,
                "host_name": name,
                "lease_id": lease_id,
                "owner": self._lease_owner,
            }
            was_ambiguous = False
            try:
                result = self._request_for(binding, "POST", "/api/omnigent-host/lease", body)
            except CodaCapacityError:
                rejected.append(f"{binding.app_id}=full")
                _logger.info("CoDA acquisition rejected app_id=%s category=full", binding.app_id)
                continue
            except CodaAmbiguousAcquireError:
                # Reconcile only by replaying the identical owner/lease CAS against
                # the same App. Never spill an ambiguous request to another App.
                was_ambiguous = True
                result = self._request_for(binding, "POST", "/api/omnigent-host/lease", body)
            acquired_id = result.get("lease_id", lease_id)
            if not isinstance(acquired_id, str) or not acquired_id:
                raise click.ClickException("CoDA lease response omitted lease_id")
            if acquired_id != lease_id:
                if was_ambiguous:
                    raise click.ClickException(
                        "CoDA ambiguous acquire resolved to a different lease generation"
                    )
                # CoDA owner-CAS returns an existing generation for this owner.
                # It definitively did not acquire our requested generation, so
                # this App is full for a NEW host and spillover is safe.
                rejected.append(f"{binding.app_id}=existing-owner-lease")
                _logger.info(
                    "CoDA acquisition rejected app_id=%s category=existing-owner-lease",
                    binding.app_id,
                )
                continue
            _logger.info("CoDA acquisition selected app_id=%s", binding.app_id)
            return f"coda:{binding.app_id}#{acquired_id}"
        raise click.ClickException(
            "CoDA pool has no available lease capacity (" + ", ".join(rejected) + ")"
        )

    def _binding_for(self, sandbox_id: str) -> tuple[CodaAppBinding, str]:
        app_id, lease_id = _parse_sandbox_id(sandbox_id)
        binding = self._registry.get(app_id)
        if binding is None:
            raise click.ClickException(
                f"CoDA sandbox targets removed or unknown app_id {app_id!r}"
            )
        return binding, lease_id

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repo_url: str | None = None,
        repo_branch: str | None = None,
        repo_name: str | None = None,
        host_config: dict[str, object] | None = None,
        agent_name: str | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        binding, lease_id = self._binding_for(sandbox_id)
        if on_stage is not None:
            on_stage("cloning")
            on_stage("starting")
        result = self._request_for(
            binding,
            "POST",
            "/api/omnigent-host/connect",
            {
                "server_url": server_url,
                "host_token": token,
                "host_id": host_id,
                "host_name": host_name,
                "host_config": host_config,
                "repo_url": repo_url,
                "repo_branch": repo_branch,
                "repo_name": repo_name,
                "lease_id": lease_id,
                "agent_name": agent_name,
            },
        )
        workspace = result.get("workspace") or self._workspace_path
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise click.ClickException(
                "CoDA connect response did not contain an absolute workspace"
            )
        return workspace

    def allocate_workspace(self, sandbox_id: str, session_id: str) -> str:
        binding, lease_id = self._binding_for(sandbox_id)
        result = self._request_for(
            binding,
            "POST",
            "/api/omnigent-host/workspaces",
            {"lease_id": lease_id, "session_id": session_id},
        )
        workspace = result.get("workspace")
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise click.ClickException("CoDA did not return an absolute session workspace")
        return workspace

    def terminate(self, sandbox_id: str) -> None:
        """Release only the granting App's lease; uncertainty is surfaced to the caller."""
        binding, lease_id = self._binding_for(sandbox_id)
        self._request_for(
            binding,
            "POST",
            "/api/omnigent-host/disconnect",
            {"lease_id": lease_id, "scrub": True},
        )
