# Handoff: last mile — mirror the agent's responses back (2026-07-08)

## Status: terminal LAUNCHES; responses don't mirror to the web transcript yet

The header-mode blocker is **solved**. A guest native Claude Code session on the
SP-owned `coding-agents` host now:
- launches its runner (tunnel authenticates as the host SP — unchanged),
- resolves its agent spec/config via the binding-token read grant (was 404),
- **starts the native Claude terminal** (`native_terminal_start_failed` GONE:
  `status: idle, runner_online: True, terminal_pending: False,
  last_task_error: None`).

**What's left:** the agent runs, but its output does not appear in the web
transcript. The claude-native **transcript/usage forwarder**
(`omnigent/claude_native_forwarder.py`) `POST`s to
`/v1/sessions/{id}/events` and gets **404**, because:

1. **The forwarder builds its OWN `httpx.AsyncClient`** (`claude_native_forwarder.py`
   ~line 770, `_post_external_session_usage` and siblings) — NOT the runner's
   `server_client` in `_entry.py` where the binding-token header was added. So
   the forwarder's requests carry no `X-Omnigent-Runner-Tunnel-Token`.
2. **`post_event`** (the `POST /v1/sessions/{id}/events` handler,
   `omnigent/server/routes/sessions.py` ~line 18548) calls
   `_require_access_and_level(... LEVEL_EDIT ...)` but was NOT threaded with
   `runner_binding_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)` — only
   the 3 GET spec-callback routes were.

The server-side grant already caps at LEVEL_EDIT (commit 40613640), which is
what `POST /events` needs — so once the header both (a) is sent by the forwarder
and (b) is read by `post_event`, the forward should 200 and responses appear.

## The last-mile change (precise)

1. **Server** (`sessions.py`, `post_event` ~18548): add
   `runner_binding_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)` to its
   `_require_access_and_level(...)` call (same one-line pattern as the 3 GET
   routes). Consider auditing OTHER runner-called write routes on the
   native path the same way (e.g. response/result posts), but `/events` is the
   one the forwarder hits.
2. **Runner forwarder** (`claude_native_forwarder.py`): the `httpx.AsyncClient`
   it builds (~770) must include the binding-token header when the prefer flag
   is set. Cleanest: pass the header into `headers=` at construction, sourced
   from `_runner_tunnel_binding_token_from_env()` gated on
   `OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT` (mirror `_entry.py`'s
   `server_client`). Find every `httpx.AsyncClient(` in the native forwarder
   path and thread it consistently — there may be more than one callback client.
3. **Audit for other runner→server clients** on the claude-native path that also
   need the header (transcript item posts, cost, status). Grep
   `httpx.AsyncClient(` and `client.post(.*/events` under `omnigent/` for the
   runner side.

## Verify (headless, unsandboxed)

Rebuild wheel with a UNIQUE `.post<ts>` version (dev0×2 won't force-reinstall),
upload to `/Volumes/lakemeter_catalog/daveok_omnigent/artifacts/wheels` (delete
old first), redeploy `coding-agents` (force-reinstall) for the forwarder change,
and redeploy `omnigent-daveok` for the `post_event` change. Then:
- create a claude-native session on `host_e5955ca57bfaf3665cbae3be6e6856d0` as
  admin (guest ≠ SP host owner), send "Reply with exactly: HELLO_FROM_CODA_OK",
  wait ~40s, and confirm an **assistant message item** with that text appears in
  the transcript (today only a `resource_event` appears).
- runner log (`[runner:*]` via `databricks apps logs coding-agents`) should show
  `POST /v1/sessions/{id}/events → 200` (was 404).

## Deploy mechanics (learned this session)

- `server_version` in `/v1/info` is base metadata, NOT the `.post` stamp — don't
  use it to check "is my code live"; `zipfile`-read the deployed wheel's `.py`.
- The `deploy.py` UC-grant tail 403 is a KNOWN cosmetic failure (David lacks
  MANAGE on the shared catalog) — the app deploys/restarts BEFORE it; exit code
  1 does not mean the deploy failed.
- App-directed `curl` flakes in-sandbox (host not allowlisted) — run E2E
  verification with the sandbox disabled.
- CoDA force-reinstall requires `OMNIGENTS_FORCE_REINSTALL=1` (already in
  `app.yaml`) + a UNIQUE wheel version.

## Commits (branch `001-external-host-owner-mint`)

`38604674` (001 mint, cookie-mode), `ef5d137a` (002 binding-token read grant +
runner header), `30b68e37` + `e589953b` (flag at all 3 launch-frame sites),
`8dfbc1bb` (runner: header not mint), `40613640` (EDIT cap). All unit +
security tests green. specs/001-* and specs/002-* carry the full speckit set.
