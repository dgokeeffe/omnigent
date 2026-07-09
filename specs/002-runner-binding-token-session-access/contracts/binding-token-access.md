# Contract: binding-token session read access

Internal contract between the runner's server callbacks and the server's read
access check. No public API change.

## Contract 1: runner sends the binding token on spec callbacks

**Direction**: runner → server (HTTP callbacks over the tunnel).

**Producer**: the runner's `server_client` (`omnigent/runner/_entry.py:~925`).
When `OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT` is set and a binding token
resolves, add header:

```
X-Omnigent-Runner-Tunnel-Token: <binding_token>
```

on all `server_client` requests (in particular
`GET /v1/sessions/{id}/agent/contents`). Unset flag → header not added
(unchanged).

## Contract 2: server grants read on a token↔session match

**Direction**: server-side access check.

**Consumer**: `_require_access_and_level_sync` / `require_access_and_level`
(`omnigent/server/routes/_auth_helpers.py`), reached from the three spec-callback
routes (`get_session`, `get_session_agent`, `get_session_agent_contents`) which
pass `runner_binding_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)`.

**Rule**:
| Condition | Result |
|---|---|
| header token present AND `required_level <= LEVEL_READ` AND `token_bound_runner_id(token) == conv.runner_id` | **grant** `SessionAccess(level=LEVEL_READ, conversation=conv)` |
| token present but runner-id doesn't match `conv.runner_id` | fall through (fail closed → today's 401/403/404) |
| token present but `required_level > LEVEL_READ` | fall through (grant can't satisfy > read) |
| token absent / empty / malformed | fall through (unchanged behavior) |

**Invariants**:
- Never returns a `user_id`; the grant is not an identity.
- Scoped to the single matched conversation; a token for session X grants
  nothing for session Y.
- Verification uses no auth-mode secret (works in header + cookie modes).
- After relaunch, `conv.runner_id` changes → the old token stops matching.

## Non-goals (contract stability)

- No change to `POST /v1/runners/{id}/token` (the cookie-mode mint) or to the
  spec-callback route paths/response shapes.
- No change to the no-token access path.
