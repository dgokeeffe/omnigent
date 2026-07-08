# Phase 0 Research: External-host runners mint a session-scoped owner token

All unknowns were resolved by direct code inspection (two Explore passes with
line citations, verified live against the deployed apps). No open NEEDS
CLARIFICATION remain.

## Decision 1: Reuse the managed-sandbox mint, don't build a new path

**Decision**: Extend the existing binding-token → owner-JWT mint to external
hosts by changing the runner's credential-selection order; do not add a new
token type, endpoint, or registration.

**Rationale**: The mechanism is already host-type-agnostic and fully wired for
external hosts:
- Server mints + binds the per-runner binding token on **every** host launch:
  `_launch_runner_on_host` (`omnigent/server/routes/sessions.py:6121`) —
  `binding_token = secrets.token_urlsafe(32)` (`:6149`),
  `token_bound_runner_id` (`:6150`), `replace_runner_id` binds
  runner_id→conversation (`:6152-6156`), frame sent (`:6188`).
- External host already passes the binding token into the runner env:
  `_build_runner_env` sets `RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR`
  (`omnigent/host/connect.py:521`), called from `_handle_launch` (`:1045-1052`).
- Mint endpoint resolves the owner from the runner→conversation binding
  regardless of host type: `POST /v1/runners/{id}/token`
  (`omnigent/server/routes/runner_tunnel.py:284-342`) →
  `_resolve_managed_runner_owner` (`omnigent/server/app.py:2199-2217`).

**Alternatives considered**:
- *Server grants the host-owner SP read on its host's sessions* (Option B) —
  rejected: weaker identity retention (runner acts as the SP machine identity;
  audit shows the SP, not the guest), and a permission-model expansion with a
  broader blast radius.
- *Per-attendee CoDA app so owner == runner identity* (Option C) — rejected as
  the general solution: avoids sharing rather than solving it; doesn't
  generalize to one shared host, many users.

## Decision 2: Server decides "session owner != host owner", runner obeys

**Decision**: The server evaluates the owner comparison at launch and sets an
optional flag on the `host.launch_runner` frame; the host threads it into the
runner env; the runner reads it.

**Rationale**: The runner's credential selection (`_make_auth_token_factory`,
`omnigent/runner/_entry.py:397-417`) runs at process startup, **before** it
fetches the session, and it holds neither owner as a comparable value — it
cannot compute the condition locally. The server, in `_launch_runner_on_host`,
has both `conv` (→ session owner) and `host_conn` (→ host owner) in scope.

**Alternatives considered**:
- *Runner tries inherited credential first, falls back to mint on a 404* —
  rejected: a 404 is ambiguous (could be a genuinely deleted session), and it
  adds retry complexity to every callback for a condition the server already
  knows cleanly.

## Decision 3: Guest-only preference (session owner != host owner)

**Decision**: Prefer the mint only when the owners differ; owner-on-own-host
keeps the inherited-credential path unchanged.

**Rationale**: Narrowest blast radius — the dominant existing usage (laptop host
serving its owner) is untouched, so regression risk is confined to the new
guest path. Matches the user's fixed constraint.

## Decision 4: Accept the full-owner minted token

**Decision**: Use the existing `mint_runner_token` → `mint_session_token(owner)`
JWT as-is. It authenticates as the session owner with full owner authority.

**Rationale**: Identical trust model to what server-managed sandboxes already
rely on today (`omnigent/server/auth.py:372-402`) — no new risk class. Fixes
the 404 and retains the guest's identity for audit. Session-scoping to
least-privilege is a separate, larger change (out of scope, noted as a
follow-up).

**Alternatives considered**:
- *Mint a session-scoped (spec-read-only) token* — deferred: requires changing
  `mint_session_token` / the auth model; larger surface; not needed to unblock.

## Decision 5: Backward compatibility via optional field + optional env var

**Decision**: The frame field defaults `False` (mirrors the existing optional
`harness` field on `HostLaunchRunnerFrame`, `omnigent/host/frames.py:91`); the
env var is only set when `True`; the runner treats absent/False as today's
behavior.

**Rationale**: Server and host deploy on independent schedules (server =
`omnigent-daveok`, host = the CoDA app installing a wheel from a UC Volume).
Version skew (new server + old host, or old server + new host) must degrade to
today's behavior, not break launches (spec SC-005 / FR-004). An old host simply
ignores an unknown frame field; a new host with an old server never sees the
flag set.

## Runner credential-selection order — the exact change site

`_make_auth_token_factory` (`omnigent/runner/_entry.py:397-417`) currently:
1. probes the user/SD credential first (`_factory()`, returns it if non-None),
2. else falls back to `_make_managed_mint_factory(server_url, binding_token)`.

The change: when the new env flag is set AND a binding token is present, return
`_make_managed_mint_factory(...)` **before** the user-credential probe.
Otherwise unchanged. Reused helpers: `_make_managed_mint_factory` (`:420`),
`_runner_tunnel_binding_token_from_env` (`:586`).
