# Contract: host.launch_runner frame + runner env (guest-identity signal)

This feature's only external interfaces are (1) the internal server↔host frame
`host.launch_runner` and (2) the host→runner process environment. Both are
internal to Omnigent; there is no public/user-facing API change.

## Contract 1: `HostLaunchRunnerFrame.prefer_binding_token_mint`

**Direction**: server → host (over the host tunnel).

**Producer**: `_launch_runner_on_host` (`omnigent/server/routes/sessions.py`).
Sets `prefer_binding_token_mint = (session_owner != host_owner)`.

**Consumer**: `_handle_launch` (`omnigent/host/connect.py`).

**Schema addition** (dataclass field):
```
prefer_binding_token_mint: bool = False
```

**Compatibility guarantees**:
- Default `False` — omitting it (old server) or an old host ignoring it both
  yield today's behavior.
- Encode/decode MUST round-trip the field and MUST tolerate its absence when
  decoding a frame produced by an older server (decode → `False`).

## Contract 2: `OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT` env var

**Direction**: host → runner (subprocess environment).

**Producer**: `_build_runner_env` (`omnigent/host/connect.py`), given the frame
flag from `_handle_launch`. Sets the var to `"1"` **only when** the flag is
True; otherwise does not set it.

**Consumer**: `_make_auth_token_factory` (`omnigent/runner/_entry.py`).

**Name**: defined in `omnigent/runner/identity.py` (constant beside
`RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR`).

**Behavioral contract**:
| Env flag | Binding token present | Runner credential |
|----------|----------------------|-------------------|
| set (`"1"`) | yes | binding-token mint (session-owner identity) — **preferred first** |
| set (`"1"`) | no | fall through to today's order (safe degrade) |
| unset | — | today's order (inherited credential first, mint as fallback) |

## Contract 3: mint endpoint (reused, unchanged)

`POST /v1/runners/{runner_id}/token` — no change. Documented here only to fix
the reused contract:
- Auth: `X-Omnigent-Runner-Tunnel-Token` header = the binding token; endpoint
  requires `token_bound_runner_id(token) == runner_id`.
- Resolves owner via the runner→conversation binding
  (`_resolve_managed_runner_owner`); returns an owner JWT for that session's
  owner. This is what lets a guest session's runner read that session's spec.

## Non-goals (contract stability)

- No change to `POST /v1/runners/{id}/token` request/response.
- No change to the spec-callback endpoints (`GET /v1/sessions/{id}`,
  `GET /v1/sessions/{id}/agent/contents`) — they already 200 for the session
  owner; this feature just makes the runner *be* the session owner.
