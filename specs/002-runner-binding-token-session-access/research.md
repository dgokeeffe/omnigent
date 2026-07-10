# Phase 0 Research: Runner binding-token session access (header-mode)

All resolved by code inspection (header-mode Explore pass + the live E2E that
proved the 001 mint path fails in header mode). No open unknowns.

## Decision 1: Why the mint path (001) fails here → use a token-match grant

**Decision**: Do not mint a session-owner JWT. Instead verify the runner's
tunnel binding token directly against the session's recorded runner and grant
read.

**Rationale**: `mint_runner_token` returns `None` unless the auth source is
`oidc`/`accounts` (`omnigent/server/auth.py:390-391`); the mint endpoint then
400s ("unsupported in this auth mode", `runner_tunnel.py:331-338`). The CoDA
server runs **header mode** (identity via `X-Forwarded-Email`,
`auth.py:468-511`), which has no cookie secret to sign/verify a JWT. Confirmed
live: server sets the 001 flag → runner calls mint → 400 → falls back to the SP
credential → spec 404. A token-match grant needs no secret and works in every
mode.

## Decision 2: The match is self-verifying, no secret

**Decision**: `runner_id = token_bound_runner_id(binding_token)` and compare to
`conv.runner_id`.

**Rationale**: `token_bound_runner_id` is pure `sha256("omnigent-runner:"+token)[:32]`
(`omnigent/runner/identity.py:108-125`) — no key. The server already stored
`conv.runner_id` at launch (`replace_runner_id`), and **already trusts this exact
predicate**: `token_bound_runner_id(token) == bound_runner_id` at
`sessions.py:12092`, and `_resolve_managed_runner_owner` resolves owner from the
same binding (`app.py:2199-2217`). So possession of the binding token already
proves "I am the runner for conversation X" everywhere else; we extend that
trust to the read-path access check.

## Decision 3: Read-only, single-session, fail-closed (security)

**Decision**: The grant returns `SessionAccess(level=LEVEL_READ, conversation=conv)`
for the matched session only, gated to `required_level <= LEVEL_READ`; any
non-match / missing / malformed token falls through to today's checks.

**Rationale**: Least-privilege — strictly narrower than 001's full-owner JWT. It
never yields a `user_id`, so it can't be reused as an identity elsewhere; it's
scoped to the one session whose `runner_id` matches; and the `<= LEVEL_READ`
gate means it can never satisfy edit/manage/owner. Fail-closed matches the
existing helper's posture.

## Decision 4: Hook the shared read-path helper (single audit point)

**Decision**: Extend `_require_access_and_level_sync` (`_auth_helpers.py:253`) +
its async wrapper (`:351`), not each route.

**Rationale**: All three spec-callback routes gate on this one helper with
`LEVEL_READ` (`sessions.py:14423, 20133, 20184`). One change, one place to
audit. The helper already fetches + returns the conversation, so the
`conv.runner_id` needed for the match is already in hand. Insert the check
**before** the `user_id is None` 401 so a header-mode runner with no user
identity is granted.

**Alternatives rejected**: a separate FastAPI dependency (duplicates the conv
fetch) or inline in each route (3 copies, drift risk).

## Decision 5: Runner attaches the token, gated by the reused flag

**Decision**: The runner adds `X-Omnigent-Runner-Tunnel-Token: <binding_token>`
to its `server_client` callbacks (`_entry.py:~925`) when
`OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT` is set (the 001 flag).

**Rationale**: The runner does NOT send the binding token on callbacks today
(only `Authorization: Bearer`, `_entry.py:245`; in header mode nothing after the
mint declines). Reusing the 001 flag keeps the new header scoped to the
guest-on-shared-host case (minimal blast radius) rather than always-on. The flag
already flows server→host→runner-env from 001.

## Confirmed: no behavior change without a token

`_require_access_and_level_sync`'s existing 401/403/404 logic is untouched when
`runner_binding_token` is absent/empty — the new branch is purely additive and
returns early only on a positive match (FR-006 / SC-004).
