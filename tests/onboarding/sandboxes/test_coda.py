from __future__ import annotations

import threading
from types import SimpleNamespace

import click
import pytest

from omnigent.onboarding.sandboxes.coda import (
    CodaAmbiguousAcquireError,
    CodaAppBinding,
    CodaCapacityError,
    CodaPoolState,
    CodaProvider,
    _safe_control_error_detail,
)


class FakeControl:
    def __init__(self, app_id: str = "legacy") -> None:
        self.app_id = app_id
        self.calls: list[tuple[str, str, object]] = []
        self.responses: dict[str, dict[str, object]] = {
            "/api/omnigent-host/status": {"ready": True},
            "/api/omnigent-host/lease": {"ok": True},
            "/api/omnigent-host/connect": {"workspace": f"/workspace/{app_id}"},
            "/api/omnigent-host/workspaces": {"workspace": f"/workspace/{app_id}/session"},
            "/api/omnigent-host/disconnect": {"released": True},
        }

    def __call__(self, method: str, path: str, body: object) -> dict[str, object]:
        self.calls.append((method, path, body))
        return self.responses[path]


def active(_name: str) -> object:
    return SimpleNamespace(compute_status=SimpleNamespace(state="ACTIVE"))


def launcher(control: FakeControl) -> CodaProvider:
    return CodaProvider(
        app_name="coda-main",
        app_url="https://coda-main.example.com",
        request_fn=control,
        app_getter=active,
    )


def pool_provider(
    controls: dict[str, FakeControl], *, state: CodaPoolState | None = None
) -> CodaProvider:
    return CodaProvider(
        apps=tuple(
            CodaAppBinding(app_id, f"name-{app_id}", f"https://{app_id}.example.com")
            for app_id in controls
        ),
        request_fns=controls,
        app_getter=active,
        pool_state=state,
    )


def test_control_error_detail_redacts_credentials() -> None:
    detail = _safe_control_error_detail(
        '{"error":"bad request","host_token":"launch-secret",'
        '"nested":{"authorization":"Bearer x"}}'
    )
    assert "launch-secret" not in detail
    assert "Bearer x" not in detail
    assert detail == (
        '{"error":"bad request","host_token":"<redacted>","nested":{"authorization":"<redacted>"}}'
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"message":"Authorization: Bearer launch-secret"}',
        '{"error":"token=launch-secret"}',
        '{"details":["host token launch-secret"]}',
        '{"password":"launch-secret"}',
    ],
)
def test_control_error_detail_redacts_credentials_in_values(raw: str) -> None:
    detail = _safe_control_error_detail(raw)
    assert "launch-secret" not in detail
    assert "<redacted>" in detail


def test_capabilities_and_legacy_identity() -> None:
    provider = launcher(FakeControl())
    assert provider.app_ids == ("coda-main",)
    assert provider.capabilities.managed_launch is True
    assert provider.capabilities.programmatic_terminate is True


def test_prepare_accepts_current_coda_status_without_ready_field() -> None:
    control = FakeControl()
    control.responses["/api/omnigent-host/status"] = {"running": False, "stage": "idle"}
    launcher(control).prepare()


def test_prepare_accepts_partial_availability() -> None:
    a = FakeControl("a")
    b = FakeControl("b")

    def getter(name: str) -> object:
        state = "STOPPED" if name == "name-a" else "ACTIVE"
        return SimpleNamespace(compute_status=SimpleNamespace(state=state))

    provider = CodaProvider(
        apps=(
            CodaAppBinding("a", "name-a", "https://a.example.com"),
            CodaAppBinding("b", "name-b", "https://b.example.com"),
        ),
        request_fns={"a": a, "b": b},
        app_getter=getter,
    )
    provider.prepare()
    assert a.calls == []
    assert b.calls == [("GET", "/api/omnigent-host/status", None)]


def test_prepare_rejects_when_no_app_is_ready() -> None:
    provider = CodaProvider(
        app_name="coda-main",
        app_url="https://coda-main.example.com",
        request_fn=FakeControl(),
        app_getter=lambda _: SimpleNamespace(compute_status=SimpleNamespace(state="STOPPED")),
    )
    with pytest.raises(click.ClickException, match="no ready CoDA Apps"):
        provider.prepare()


def test_round_robin_provision_and_persisted_app_id() -> None:
    controls = {"a": FakeControl("a"), "b": FakeControl("b")}
    provider = pool_provider(controls)
    provider.set_lease_owner("owner@example.com")
    first = provider.provision("host-1")
    second = provider.provision("host-2")
    assert first.startswith("coda:a#")
    assert second.startswith("coda:b#")
    assert controls["a"].calls[-1][2]["app_name"] == "name-a"
    assert controls["b"].calls[-1][2]["app_name"] == "name-b"


def test_full_and_unavailable_apps_spill_new_acquisition_only() -> None:
    class Full(FakeControl):
        def __call__(self, method: str, path: str, body: object) -> dict[str, object]:
            self.calls.append((method, path, body))
            if path.endswith("/lease"):
                raise CodaCapacityError("full")
            return self.responses[path]

    controls: dict[str, FakeControl] = {"a": Full("a"), "b": FakeControl("b")}
    provider = pool_provider(controls)
    sandbox_id = provider.provision("host")
    assert sandbox_id.startswith("coda:b#")
    assert any(path.endswith("/lease") for _, path, _ in controls["a"].calls)


def test_existing_owner_generation_is_full_for_new_host_and_spills() -> None:
    a = FakeControl("a")
    a.responses["/api/omnigent-host/lease"] = {"lease_id": "existing"}
    b = FakeControl("b")
    sandbox_id = pool_provider({"a": a, "b": b}).provision("new-host")
    assert sandbox_id.startswith("coda:b#")
    assert all(path != "/api/omnigent-host/disconnect" for _, path, _ in a.calls)


def test_auth_or_malformed_probe_failure_does_not_spill() -> None:
    class Unauthorized(FakeControl):
        def __call__(self, method: str, path: str, body: object) -> dict[str, object]:
            self.calls.append((method, path, body))
            if path.endswith("/status"):
                raise click.ClickException("CoDA control request failed (401)")
            return self.responses[path]

    controls = {"a": Unauthorized("a"), "b": FakeControl("b")}
    with pytest.raises(click.ClickException, match="401"):
        pool_provider(controls).provision("host")
    assert controls["b"].calls == []


def test_ambiguous_acquire_retries_same_app_and_never_spills() -> None:
    class AmbiguousThenSuccess(FakeControl):
        attempts = 0

        def __call__(self, method: str, path: str, body: object) -> dict[str, object]:
            self.calls.append((method, path, body))
            if path.endswith("/lease"):
                self.attempts += 1
                if self.attempts == 1:
                    raise CodaAmbiguousAcquireError("lost response")
                return {"lease_id": body["lease_id"]}
            return self.responses[path]

    controls = {"a": AmbiguousThenSuccess("a"), "b": FakeControl("b")}
    sandbox_id = pool_provider(controls).provision("host")
    lease_calls = [call for call in controls["a"].calls if call[1].endswith("/lease")]
    assert sandbox_id.startswith("coda:a#")
    assert len(lease_calls) == 2
    assert lease_calls[0][2]["lease_id"] == lease_calls[1][2]["lease_id"]
    assert controls["b"].calls == []


def test_ambiguous_acquire_resolving_to_other_generation_never_spills() -> None:
    class AmbiguousThenOther(FakeControl):
        attempts = 0

        def __call__(self, method: str, path: str, body: object) -> dict[str, object]:
            self.calls.append((method, path, body))
            if path.endswith("/lease"):
                self.attempts += 1
                if self.attempts == 1:
                    raise CodaAmbiguousAcquireError("lost")
                return {"lease_id": "other-generation"}
            return self.responses[path]

    b = FakeControl("b")
    with pytest.raises(click.ClickException, match="ambiguous acquire"):
        pool_provider({"a": AmbiguousThenOther("a"), "b": b}).provision("host")
    assert b.calls == []


def test_second_ambiguous_acquire_fails_closed() -> None:
    def ambiguous(_method: str, path: str, _body: object) -> dict[str, object]:
        if path.endswith("/lease"):
            raise CodaAmbiguousAcquireError("lost")
        return {"ready": True}

    b = FakeControl("b")
    provider = CodaProvider(
        apps=(
            CodaAppBinding("a", "name-a", "https://a.example.com"),
            CodaAppBinding("b", "name-b", "https://b.example.com"),
        ),
        request_fns={"a": ambiguous, "b": b},
        app_getter=active,
    )
    with pytest.raises(CodaAmbiguousAcquireError):
        provider.provision("host")
    assert b.calls == []


def test_lifecycle_routes_only_to_granting_app() -> None:
    controls = {"a": FakeControl("a"), "b": FakeControl("b")}
    provider = pool_provider(controls)
    provider.allocate_workspace("coda:b#lease-b", "session")
    provider.terminate("coda:a#lease-a")
    assert [path for _, path, _ in controls["a"].calls] == [
        "/api/omnigent-host/disconnect"
    ]
    assert [path for _, path, _ in controls["b"].calls] == [
        "/api/omnigent-host/workspaces"
    ]


def test_removed_app_id_fails_before_http() -> None:
    control = FakeControl("a")
    provider = pool_provider({"a": control})
    with pytest.raises(click.ClickException, match="removed or unknown app_id"):
        provider.terminate("coda:removed#lease")
    assert control.calls == []


def test_shared_pool_state_is_concurrency_safe() -> None:
    state = CodaPoolState()
    controls = {"a": FakeControl("a"), "b": FakeControl("b")}
    providers = [pool_provider(controls, state=state) for _ in range(2)]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def acquire(provider: CodaProvider) -> None:
        barrier.wait()
        results.append(provider.provision("host"))

    threads = [threading.Thread(target=acquire, args=(provider,)) for provider in providers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {result.split("#", 1)[0] for result in results} == {"coda:a", "coda:b"}
