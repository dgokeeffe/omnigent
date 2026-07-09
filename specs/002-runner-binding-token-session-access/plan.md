# Implementation Plan: Runner binding-token session access (header-mode)

**Branch**: `002-runner-binding-token-session-access` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/002-runner-binding-token-session-access/spec.md`

## Summary

Let a runner read ITS OWN bound session's spec/config by presenting its tunnel
binding token, verified by the server as a pure `sha256`-derived runner-id match
against `conv.runner_id` — no auth-mode secret. This makes guest sessions on a
shared externally-owned host work in **header/proxy auth mode** (where the
cookie-based owner-JWT mint from feature 001 returns None). The grant is
read-only and scoped to the single matched session.

**Approach**: two touch points. (1) **Server**: extend the shared read-path
access helper `_require_access_and_level_sync` (`_auth_helpers.py`) to accept an
optional runner binding token; when present and `required_level <= LEVEL_READ`,
fetch the conv and if `token_bound_runner_id(token) == conv.runner_id` return a
read grant — *before* the `user_id is None` 401, so it works with no user
identity. Thread the header at the 3 spec-callback call sites. (2) **Runner**:
attach `X-Omnigent-Runner-Tunnel-Token: <binding_token>` on the `server_client`
callbacks, gated by the existing `prefer_binding_token_mint` flag (reused from
001 — its meaning becomes "authenticate callbacks with the binding token").

## Technical Context

**Language/Version**: Python 3.10+

**Primary Dependencies**: FastAPI (server), httpx (runner callbacks), existing
permission/access helpers + `token_bound_runner_id`.

**Storage**: None new. Reuses `conv.runner_id` (set at launch) + the existing
runner→conversation binding.

**Testing**: pytest (unit + security). Live E2E on the header-mode
`omnigent-daveok` + `coding-agents` deployment via the headless API + runner-log
tailer.

**Target Platform**: Databricks Apps (header auth mode) — the deployment this
feature targets; also correct in cookie modes.

**Constraints**: Grant MUST be read-only + single-session + fail-closed; MUST
NOT change no-token access outcomes; MUST NOT depend on an auth-mode secret.

**Scale/Scope**: ~2 files changed (server helper + runner client), plus the
call-site header threading. Builds on branch `001-external-host-owner-mint`.

## Constitution Check

Constitution is the unfilled template — no project-specific gates. General
gates met: reuse over new (no new endpoint/token/store; reuses the existing
binding + helper), least-privilege (read-only, single-session — narrower than
001's full-owner JWT), backward-compatible (no-token path unchanged, opt-in via
the reused flag). No violations; Complexity Tracking empty.

## Project Structure

### Documentation (this feature)

```text
specs/002-runner-binding-token-session-access/
├── plan.md              # This file
├── research.md          # Phase 0 (consolidated from the header-mode Explore pass)
├── data-model.md        # Phase 1 (binding-token grant; no persistence)
├── quickstart.md        # Phase 1 (header-mode E2E + security checks)
├── contracts/
│   └── binding-token-access.md   # the header + access-grant contract
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
omnigent/
├── server/routes/
│   ├── _auth_helpers.py     # extend _require_access_and_level_sync + async wrapper: optional runner_binding_token → read grant on token↔conv.runner_id match
│   └── sessions.py          # 3 spec-callback call sites pass request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
└── runner/
    └── _entry.py            # server_client attaches X-Omnigent-Runner-Tunnel-Token when the prefer flag is set

tests/
├── server/ (extend the _auth_helpers / sessions access tests)
└── runner/ (extend test_runner_entry for the header attach)
```

**Structure Decision**: Single-project; confined to the existing access-helper
and runner-client code. No new modules.

## Complexity Tracking

> No violations.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| (none)    | —          | —                                    |

## Key implementation notes (from research)

- **Hook**: `_require_access_and_level_sync` (`_auth_helpers.py:253`). Insert the
  binding-token check after the `permission_store is None` short-circuit and
  **before** the `user_id is None` 401 (so a no-user runner in header mode is
  granted). Guard: `if runner_binding_token and required_level <= LEVEL_READ:`
  → `conv = conversation_store.get_conversation(conversation_id)`; if
  `conv and conv.runner_id and token_bound_runner_id(runner_binding_token.strip())
  == conv.runner_id:` return `SessionAccess(level=LEVEL_READ, conversation=conv)`.
  Otherwise fall through unchanged (fail closed). Import `LEVEL_READ`
  (auth.py:76) + `token_bound_runner_id` (runner/identity.py).
- **Async wrapper** `require_access_and_level` (`:351`): add the param, pass it
  through `asyncio.to_thread`.
- **Call sites** (`sessions.py`): `get_session` (~:14423), `get_session_agent`
  (~:20133), `get_session_agent_contents` (~:20184) — pass
  `runner_binding_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)`
  (`RUNNER_TUNNEL_TOKEN_HEADER` already imported at sessions.py:117). The runner
  only strictly needs `/agent/contents`, but all three share the helper so all
  get it consistently.
- **Runner** (`_entry.py:~925`): on the `server_client` `headers=`, add
  `RUNNER_TUNNEL_TOKEN_HEADER: <binding_token>` when
  `os.environ.get(RUNNER_PREFER_BINDING_TOKEN_MINT_ENV_VAR)` and a binding token
  resolves. Keeps blast radius to the guest-on-shared-host case.
- **Reused**: `token_bound_runner_id` (identity.py:108), `conv.runner_id`,
  `RUNNER_TUNNEL_TOKEN_HEADER` (identity.py:22), the `prefer_binding_token_mint`
  flag wiring from 001, `SessionAccess`.
- **NOT used**: the JWT mint (`mint_runner_token` / `_make_managed_mint_factory`)
  — left in place for cookie-mode managed sandboxes; not on this path.
