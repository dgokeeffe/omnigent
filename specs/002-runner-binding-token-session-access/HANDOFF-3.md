# Handoff — CoDA remote host: PR raised, gateway-401 remains (2026-07-09, session 3)

## One-paragraph state

The **binding-token mirroring is fixed, verified, and now packaged as a clean PR** on the
omnigent fork. A guest native-Claude session on a shared SP-owned Databricks App host
launches its terminal AND mirrors the agent's response back to the web transcript. The
**one remaining blocker** to a fully-working shared CoDA host is native Claude's auth to
the Databricks AI Gateway (a `Please run /login · 401 Credential ... unsupported type`) —
a CoDA-side / permissions issue, NOT anything the omnigent PR fixes.

## Is this "everything to connect CoDA as a remote host"? — NO. Four pieces:

| Piece | Where | Status |
|---|---|---|
| CoDA registers as a host (WS tunnel, routing) | omnigent `main` (pre-existing) | ✅ done, untouched by this work |
| Guest (non-owner) can run a session on it | **omnigent PR #2 (this session)** | ✅ done + verified |
| CoDA-side boot (install claude/tmux on SP path, force-reinstall, log tailer, version marker, claude-settings) | CoDA repo `feat/runner-log-observability` | ⚠️ committed LOCAL only, **can't push** |
| Native Claude ↔ Databricks gateway auth | CoDA-side apiKeyHelper / SP `CAN_QUERY` grant | ⚠️ **still 401 — the last blocker** |

## PRs

### PR A — RAISED ✅
- **https://github.com/dgokeeffe/omnigent/pull/2** — base `dgokeeffe/omnigent:main` ← `pr/guest-session-runner-auth`.
- **On the FORK only, NOT the public `omnigent-ai/omnigent` upstream** (per standing instruction). To upstream it later: change base to `omnigent-ai/omnigent:main`, head `dgokeeffe:pr/guest-session-runner-auth`.
- **1 commit / 30 files / +2222/−8**, all feature tests green (4 `test_connect.py` cases need UNSANDBOXED — they write `~/.omnigent/logs`), lint+format clean, no admin-host-mgmt code.
- Clean **001+002 unit** (002 depends on 001's `prefer_binding_token_mint` flag, so they can't be split further). Kept 001's JWT mint (it's the live OIDC-mode path; 002's binding-token grant is the header-mode path — complementary, not redundant).
- **GOTCHA hit + fixed**: the fork's `main` was 781 commits stale, so GitHub first showed a bogus 100-commit / +348k diff. Fix: `git push fork main:main` (sync fork to upstream e83b11ea) THEN **close+reopen the PR** to force merge-base recompute. A base sync alone did NOT refresh the cached diff; the close/reopen did.

### PR B — NOT raised (separate, older feature)
- **admin host-management** (7 commits `0494914e`→`0178515d`): admin fleet view, host share/shutdown/grants, web UI. Lives only in the full 21-commit branch `001-external-host-owner-mint`. Independent of PR A. Split it out the same way (branch off main, apply `git diff <its-base>..0178515d` for its files) if/when you want it upstreamed. It touches `omnigent/cli.py` + the admin hunks of `sessions.py`/`hosts.py` that PR A deliberately excluded.

### CoDA PR — BLOCKED (task #5)
- `coding-agents-databricks-apps` branch `feat/runner-log-observability` (10 commits incl. `1473add` claude-settings, `16d8e97` version marker). **No writable remote**: origin(databrickslabs)=403 EMU, dgokeeffe fork=ARCHIVED read-only, private=404. Needs a fresh fork / unarchive before it can be pushed or PR'd.

## Branch / repo map
- **omnigent** (`~/Repos/omnigent`): `pr/guest-session-runner-auth` (= PR #2, clean), `001-external-host-owner-mint` (full 21-commit backup, pushed to fork). Local+upstream main = `e83b11ea`.
- **CoDA** (`~/Repos/coding-agents-databricks-apps`): `feat/runner-log-observability` (local only, unpushable).

## THE remaining task — fix the gateway-401 (fully working demo)
Native Claude on `coding-agents-2` mirrors but can't answer: `Please run /login · API Error: 401 Credential was not sent or was of an unsupported type [ReqId: ...]`. `settings.json` IS written (gateway base URL + apiKeyHelper present) — the SP credential is rejected. ONE decisive check, in the `coding-agents-2` browser terminal (Okta SSO):
```
python3 ~/.claude/anthropic-token-helper.py
```
- **Empty / exit 1** → apiKeyHelper's `Config(profile="omnigents-host")` doesn't resolve in the runner subprocess's HOME → returns no token. Fix in CoDA `setup_claude.py` / `_ensure_claude_settings` so the omnigents-host profile (or SP token) is reachable where the helper runs.
- **A token printed** → the app SP `08437ab8-0633-4678-b55b-6146eb4e8e07` lacks `CAN_QUERY` on the workspace serving endpoints. Grant it (databricks CLI / permissions API). No code change.
Verify: re-run `<scratchpad>/e2e_verify.py` UNSANDBOXED; assistant message should contain `HELLO_FROM_CODA_OK`, not `/login`.

## Live deployment state (unchanged this session)
- Server `omnigent-daveok`: RUNNING, deployment `01f17b4c`, CLEAN (debug reverted, both binding-token fixes live).
- Host `coding-agents-2`: online, commit `690b5110` + claude-auth fix `1473add`. host_id `host_1ee08781a0aefdd7dc7a8e5c3552e84f`, SP `08437ab8-...`. Agent `ag_58a1bc5bf0bba6d31ceeb7661f8d751c`, workspace `/app/python/source_code`.

## Deploy gotchas (carried forward, all in memory coda-native-claude-bwrap-blocker.md)
- **`deploy.py` does NOT redeploy the server app** — it uploads the wheel but `bundle run` only restarts the current deployment. After `deploy.py`, ALWAYS run `databricks apps deploy omnigent-daveok --source-code-path /Workspace/Users/david.okeeffe@databricks.com/.bundle/omnigent/omnigent-daveok/files/src --profile lakemeter --no-wait` and confirm a NEW deployment id via `apps list-deployments`. This cost the entire session-2.
- Run all `databricks bundle`/`apps` + app-directed `curl` + `gh`/`git push` UNSANDBOXED (token-cache write / host not allowlisted / network → `exit status 161` or `operation not permitted` in-sandbox).
- Server app logs are invisible (OTel swallows module loggers + print; only uvicorn access log surfaces) — diagnose server-side via HTTP response body; host-side via `databricks apps logs coding-agents-2` grepping a unique marker fast (zombie-runner flood).
- Trust local `git merge-base`/diff over GitHub's cached PR numbers (they lag a stale-base open by minutes).
