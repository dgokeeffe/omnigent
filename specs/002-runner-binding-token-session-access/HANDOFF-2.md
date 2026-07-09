# Handoff — CoDA guest-session mirroring (2026-07-09, session 2)

## TL;DR
The **binding-token mirroring is FIXED and VERIFIED** — a guest native-Claude
session on the SP-owned shared `coding-agents-2` host now mirrors the agent's
response back to the web transcript. The multi-session goal is met.

**One separate issue remains** (downstream, not the binding-token task): native
Claude Code on the CoDA host reaches the Databricks AI Gateway but gets a **401
"Credential was not sent or was of an unsupported type"** — so it can mirror but
can't produce a real answer yet.

---

## What was actually wrong (the root cause that ate the session)

`deploy/databricks/deploy.py` **never actually redeploys the server app.** It runs
`databricks bundle deploy` (uploads the wheel to
`/Workspace/Users/david.okeeffe@databricks.com/.bundle/omnigent/omnigent-daveok/files/src`)
+ `databricks bundle run` (only START/restart of the CURRENT deployment). It never
creates a new **app deployment** from the uploaded source. So the server ran
**pre-fix code for the entire session** — every 404 was against stale code.

**Correct server redeploy** (do this AFTER `deploy.py` builds+uploads, OR fix
deploy.py to call it):
```
databricks apps deploy omnigent-daveok \
  --source-code-path /Workspace/Users/david.okeeffe@databricks.com/.bundle/omnigent/omnigent-daveok/files/src \
  --profile lakemeter --no-wait
```
Then verify a NEW deployment id appears via `databricks apps list-deployments
omnigent-daveok` and reaches SUCCEEDED.

## Two observability traps (both proven, both recorded in memory)
1. **Server app logs are invisible.** `omnigent.*` module loggers, raw `print()`,
   and even `uvicorn.error` from app code are ALL swallowed by
   `opentelemetry-instrument` (→ stderr, discarded; no OTLP endpoint on lakemeter).
   ONLY uvicorn's own access log (`INFO:  10.x - "POST /events" 404`) surfaces in
   `databricks apps logs`. For server-side debug, use the **HTTP response body**
   (a temporary field on a known-reachable route like `/v1/info`), not logs.
2. **CoDA host runner logs ARE visible** via `databricks apps logs coding-agents-2`
   (the log-tailer, commit a2021dc) BUT the tail is a rolling buffer flooded by
   ZOMBIE runners (each guest session spawns a runner that lives 3600s past the
   session, spamming 404s). Grep for a UNIQUE marker within seconds, or read the
   raw file from the browser terminal.
3. The Apps proxy does **NOT** strip the custom `X-Omnigent-Runner-Tunnel-Token`
   header (verified via a `/v1/info` echo — present, len=22).

## The fixes (all committed)
**omnigent, branch `001-external-host-owner-mint`** (pushed to `dgokeeffe/omnigent`):
- `8455cfcb` — `post_event` (`POST /events`) threads the binding token into
  `_require_access_and_level`.
- `690b5110` — `evaluate_policy` (`/policies/evaluate`, the policy hook) +
  `claude_permission_request_hook` thread it too. These were the two routes the
  first fix missed; found via code-trace subagent.
  → 6 threaded-token sites total (3 GET spec routes + post_event + evaluate_policy
  + permission_request_hook), all verified in the deployed wheel.
- Runner side (already in `8455cfcb`, `runner/app.py:5448-5450`): forwarder +
  hook headers get the binding token when `OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT`
  is set. This was correct all along; the runner was never the problem.

**CoDA, branch `feat/runner-log-observability`** (committed LOCAL only — see remotes):
- `16d8e97` — boot-version marker: logs `OMNIGENT VERSION INSTALLED: ... (<commit>)`
  to host stdout. ESSENTIAL diagnostic — grep it to confirm the live commit after
  a force-reinstall. (`omnigent --version` embeds the git commit; package version
  stays `dev0`, the `.post<ts>` is only in the wheel filename.)
- `1473add` — `_ensure_claude_settings(sp_creds)`: on the auto host-connect path,
  mints an SP bearer via `_sp_bearer` and runs `setup_claude.py` with
  `DATABRICKS_TOKEN` set → writes `~/.claude/settings.json` (gateway `/anthropic`
  base URL + spec-C apiKeyHelper). Wired into `_supervise` after `_ensure_claude()`.
  Verified: `setup_claude.py rc=0; settings.json exists=True` in host logs.

## Current LIVE state (verified this session)
- **Server `omnigent-daveok`**: deployment `01f17b4c` SUCCEEDED, RUNNING, CLEAN
  (temporary `/v1/info debug_headers` + runner diagnostic reverted; confirmed gone).
  Both binding-token fixes live.
- **Host `coding-agents-2`** (NEW app; old `coding-agents` slot was deleted/wedged):
  online, commit `690b5110`, native-claude:True, claude-auth fix `1473add` live.
  host_id `host_1ee08781a0aefdd7dc7a8e5c3552e84f`, SP owner
  `08437ab8-0633-4678-b55b-6146eb4e8e07`.
- E2E driver: `<scratchpad>/e2e_verify.py` — resolves host by name `coding-agents-2`,
  creates guest claude-native session (agent `ag_58a1bc5bf0bba6d31ceeb7661f8d751c`,
  workspace `/app/python/source_code`), sends `HELLO_FROM_CODA_OK`, asserts an
  assistant message item. Run UNSANDBOXED (`uv run python -u <driver>`).

## What the last E2E showed (the remaining issue)
3 transcript items (resource_event + message/user + message/assistant) → mirroring
WORKS. But the assistant text = `"Please run /login · API Error: 401 Credential
was not sent or was of an unsupported type [ReqId: ...]"`. So Claude reaches the
gateway (settings.json is correct) but its credential is rejected.

**Leading hypotheses (need the browser terminal or a permissions check to decide):**
1. apiKeyHelper's `Config(profile="omnigents-host").authenticate()` doesn't resolve
   in the runner subprocess's HOME → returns empty → Claude sends no credential → 401.
2. The app SP `08437ab8-...` lacks `CAN_QUERY` on the workspace serving endpoints.

**The ONE decisive check** (in the `coding-agents-2` browser terminal, Okta SSO):
```
python3 ~/.claude/anthropic-token-helper.py
```
- Empty stdout / exit 1 → profile-resolution bug → fix in `setup_claude.py`'s
  `_sp_bearer`/`SP_PROFILE` handling or ensure the profile is written where the
  runner reads it.
- A token on stdout → it's a CAN_QUERY grant issue → grant the SP query on the
  serving endpoints.

## Open PR / branch actions
- **omnigent branch is PUSHED to `dgokeeffe/omnigent`, PR HELD** (David's call).
  20 commits / 3 features: admin-host-mgmt + feature 001 (cookie-mode owner-token
  mint) + feature 002 (binding-token). DECISIONS before PR: (a) scope — one big PR
  vs split 002-only; (b) keep-or-drop 001's cookie-mode mint. RECOMMENDATION: keep
  001 (covers accounts/OIDC mode; 002's header-mode grant doesn't).
- **CoDA branch CANNOT be pushed**: origin(databrickslabs)=403 EMU,
  dgokeeffe fork=ARCHIVED read-only, private=404, datasciencemonkey=not mine.
  Needs a writable remote (new fork / unarchive) before backup or PR.

## Working-tree note
`pyproject.toml` (+ sdks/*/pyproject.toml) show dirty = the `--keep-version-bump`
residue (`0.5.0.post1783570339`). Do NOT commit that; `git checkout` them or leave
for `--allow-dirty`. The `.png` files are unrelated CoDA-deck artifacts.

## Deploy gotchas (carried forward, all in memory)
- Run `deploy.py` / any `databricks bundle`/`apps` command UNSANDBOXED
  (token-cache write; sandbox → `exit status 161`). App-directed curl also
  unsandboxed (host not in sandbox allowlist).
- Server-side diagnostics: use response-body, not logs. Host-side: grep a unique
  marker fast (zombie flood).
- The `deploy.py` UC-grant tail 403 AND the `bundle run` "Must specify environment
  variable source" error are BOTH cosmetic — they fire AFTER the app is RUNNING.
