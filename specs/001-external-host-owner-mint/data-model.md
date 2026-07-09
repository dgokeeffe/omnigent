# Phase 1 Data Model: External-host runners mint a session-scoped owner token

This feature introduces no persistent storage or schema changes. The "entities"
here are the in-flight data carriers that thread the launch-time decision from
server → host → runner.

## Entity: `HostLaunchRunnerFrame` (extended)

The server→host frame that instructs a host to spawn a runner.
Defined in `omnigent/host/frames.py` (dataclass, ~line 91).

| Field | Type | Existing? | Notes |
|-------|------|-----------|-------|
| `request_id` | str | existing | correlation id |
| `binding_token` | str | existing | per-runner tunnel binding token; runner derives `runner_id` and mints against it |
| `workspace` | str | existing | runner cwd on the host |
| `harness` | str \| None = None | existing | canonical harness; optional (fail-open) |
| **`prefer_binding_token_mint`** | **bool = False** | **NEW** | when True, the runner should prefer the binding-token mint (session-owner identity) over the inherited host-owner credential |

**Validation / compatibility**: defaults to `False`. Older hosts that decode the
frame ignore the unknown field; a new host with an old server never receives it
set. No other field changes.

## Entity: runner env var (new)

The host writes the flag into the spawned runner's environment.
Name defined in `omnigent/runner/identity.py`, beside
`RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR`.

| Env var | Value | Set when |
|---------|-------|----------|
| `OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT` | `"1"` | only when `prefer_binding_token_mint` is True on the frame |

**Rule**: absent ⇒ today's behavior (inherited credential preferred). The runner
reads it in `_make_auth_token_factory`.

## Entity: session-owner credential (reused, not new)

A short-lived owner JWT representing the **session owner**, obtained by the
runner from `POST /v1/runners/{id}/token` using its binding token. Minted by
`mint_runner_token` → `mint_session_token(owner)` (`omnigent/server/auth.py`).
Cached + re-minted near expiry by the existing `_ManagedMintTokenFactory`
(`omnigent/runner/_entry.py:473`). No change to this entity — only *when* the
runner chooses to use it.

## Derived / decision data (server-side, transient)

| Value | Source at launch | Used for |
|-------|------------------|----------|
| session owner | `get_session_owner(conv)` in `_launch_runner_on_host` | the comparison |
| host owner | the `host_conn` / host record `owner` | the comparison |
| `prefer_binding_token_mint` | `session_owner != host_owner` | set on the frame |

## State / flow

```
server _launch_runner_on_host:
    prefer = (session_owner != host_owner)
    frame.prefer_binding_token_mint = prefer
        │
        ▼  host.launch_runner (tunnel)
host _handle_launch → _build_runner_env:
    if frame.prefer_binding_token_mint:
        env[OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT] = "1"
        │
        ▼  subprocess env
runner _make_auth_token_factory:
    if env flag set and binding token present:
        return _make_managed_mint_factory(...)   # session-owner identity
    else:
        <today's order: inherited credential, mint as fallback>
```

No state machine, no persistence, no migration.
