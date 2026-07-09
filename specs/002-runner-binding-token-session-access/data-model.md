# Phase 1 Data Model: Runner binding-token session access (header-mode)

No persistence or schema change. The "entities" are the request-time inputs and
the transient grant.

## Entity: runner binding token (existing, reused)

The per-launch secret the server issues (`secrets.token_urlsafe(32)` in
`_launch_runner_on_host`) and the host passes to the runner env
(`OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN`). Carried on callbacks in the header
`X-Omnigent-Runner-Tunnel-Token` (`RUNNER_TUNNEL_TOKEN_HEADER`).

- **Derived runner id**: `token_bound_runner_id(token)` =
  `sha256("omnigent-runner:" + token)[:32]` → `runner_token_<digest>`. Pure
  function; no secret.

## Entity: session's recorded runner (existing)

`conv.runner_id` — the runner id the server stored on the conversation at launch
(via `replace_runner_id`). The match target.

## Entity: binding-token access grant (new, transient — not stored)

Produced only when `token_bound_runner_id(header_token) == conv.runner_id` and
the required level is `<= LEVEL_READ`.

| Property | Value |
|---|---|
| level | `LEVEL_READ` (1) |
| conversation | the matched `conv` |
| user identity | **none** (never yields a user_id) |
| scope | the single matched session only |
| lifetime | request-scoped (recomputed per request) |

## Decision flow (in `_require_access_and_level_sync`)

```
if permission_store is None: return open           # unchanged
# NEW: binding-token grant, before the user_id 401
if runner_binding_token and required_level <= LEVEL_READ:
    conv = conversation_store.get_conversation(conversation_id)
    if conv and conv.runner_id and \
       token_bound_runner_id(runner_binding_token.strip()) == conv.runner_id:
        return SessionAccess(level=LEVEL_READ, conversation=conv)
    # else fall through (fail closed)
if user_id is None: raise 401                       # unchanged from here down
... existing admin / grant / 403 / 404 logic ...
```

No state machine, no migration, nothing persisted.
