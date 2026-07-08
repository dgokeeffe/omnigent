# Admin Host-Management — Verification Phase (D) Handoff

**For the next session.** Continues the verification & evaluation phase against the
deployed fork (`omnigent-daveok` on lakemeter, branch `feat/admin-host-management`,
PR `dgokeeffe/omnigent#1`). Read `specs/admin-host-management-eval-spec.md` for the
full done-criteria (D-S1..D-S7); this file is the *current state* + the *next moves*.

## Hard constraint this session must respect

**The user is away from their computer and CANNOT approve Okta Verify / FastPass.**
Every browser-SSO and interactive-`omnigent login` route is therefore OFF THE TABLE.
Do NOT attempt chrome-devtools navigation to the app or `omnigent login <apps-url>` —
both dead-end at the Okta wall. Work only the bearer-token REST path and local
server+host runs (neither touches Okta).

Other standing constraints (unchanged): never restart/shut down the laptop daemon
pid-451 (it hosts the Claude Code session); old-build hosts no-op `host.shutdown` by
design; the `lakemeter:` target block in `deploy/databricks/databricks.yml` stays
local-only (never pushed).

## Done-criteria status

| Criterion | State |
|---|---|
| D-S1 CI green + PR open | ✅ PR #1 ready (not draft); CI/E2E/web/Lint/Integration/UI-Snapshot/Windows all green. The 2 red checks are non-code: `Maintainer Approval` (pull_request_target governance gate, repo-owner clears in UI) and `E2E UI Required` (AI-judge gateway infra flake — `E2E UI Tests` itself passed). |
| D-S3 CoDA's own share_and_launch | ✅ grant leg 200 from CoDA's real code (`coding-agents` → `POST /api/omnigent-host/share`); launch leg 422 = documented CoDA-side empty-body shape mismatch, out of scope (CoDA follow-up). |
| D-S6 audit lines in prod logs | ✅ grant+revoke observed via `databricks apps logs omnigent-daveok -p lakemeter` (actor/target/principal/level). Browser `/logz` stream is Okta-walled; CLI logs are the equivalent. |
| D-S7 review + isolation + upstream | ✅ fresh-context review: no cross-user vuln, no High/Critical; 1 fix applied (host.fleet.list audit, commit 0178515d); isolation paragraph + fork-vs-upstream decision both in PR body. |
| **D-S2 admin screens in a browser** | ⚠️ component-tested only; live browser render BLOCKED on Okta. See E2' below for the Okta-free half. |
| **D-S4 second human grantee** | ❌ needs a willing lakemeter colleague (not Okta — a person). Deferred. |
| **D-S5 new-build remote shutdown over ingress** | ⚠️ SPLIT: remote endpoint proven today (200 + frame enqueued over the ingress); new-build daemon-exit proven locally last session. The composed "new-build daemon exits over the Apps ingress" never ran — the CoDA-restart route is blocked (see below). |

## Why the CoDA-restart route for D-S5 is dead (don't retry it)

Restarting `coding-agents` does NOT get a new-build host. Root cause:
`omnigents_host.py:234` — `ensure_installed()` returns early if the `omni` binary
already exists, and `uv tool install` writes to a **persistent** tools dir in the
CoDA sandbox (`/app/python/source_code/.local/share/uv/tools/omnigent` + shims
`.local/bin/{omnigent,omni}`) that survives restart. The volume wheel-swap (already
done: volume holds only new-build `post1783427251` wheels) is never consulted, so the
stale old-build binary reruns and no-ops `host.shutdown`. Verified: restarted the app
(deploy SUCCEEDED, host reconnected online), fired shutdown (endpoint 200), daemon did
NOT exit — `last_seen` kept advancing = old-build no-op.

Clearing the stale binary would need a shell command ON the CoDA host, but the host
REST API is **read + mkdir only** (no delete/exec — this is the C-O3 isolation property
working as designed), `sys_session_send` reaches only my session's children, and
`sys_terminal_*`/`sys_os_shell` target my local runner's host. The only way to run the
`rm` is to interactively drive the CoDA-host session through its terminal WS in a
browser — which needs Okta. So this route is Okta-gated after all.

## Next moves — all Okta-free, do these

### Task A (was E5) — local new-build shutdown, full transcript. HIGH VALUE.
Prove the new-build daemon-exit path end-to-end against a LOCAL server+host (both from
this repo's source = the new build). This + today's remote-enqueue compose to cover the
whole D-S5 path; the only leg not directly shown is the ingress transport, and the
ingress already carries `host.shutdown` (today's CoDA endpoint enqueued it; same
transport as launch/list_dir which work over that ingress).

Recipe (isolated HOME + data dir so it can't collide with pid-451's registry; local
server needs no auth):
```
export FAKE=/tmp/claude/e5-local; rm -rf $FAKE; mkdir -p $FAKE
# 1. local server on a spare port, single-user (no auth wall)
(HOME=$FAKE OMNIGENT_LOCAL_SINGLE_USER=1 uv run omnigent server --port 8791 > $FAKE/server.log 2>&1 &)
#    wait for /health to 200
# 2. local host daemon from THIS repo's source (new build) against it
(HOME=$FAKE uv run omnigent host --server http://localhost:8791 --non-interactive > $FAKE/host.log 2>&1 &)
#    wait for "Connected as ... Listening for sessions" in host.log; grab host_id from GET /v1/hosts?all=true
# 3. fire shutdown, capture the exit signature
curl -sS -X POST http://localhost:8791/v1/hosts/<HOST_ID>/shutdown
#    assert ALL THREE:
#      - host.log contains "Host shut down by server"
#      - the daemon process has exited (pgrep -f "omnigent host --server http://localhost:8791" → empty)
#      - GET /v1/hosts?all=true shows the host offline (after liveness TTL) OR the row's status flips
# 4. grep server.log for the `audit: {"action": "host.shutdown"...}` line (actor+target) → D-S6 shutdown leg
# 5. pkill the local server; rm -rf $FAKE
```
Capture the full transcript into the PR body / a comment as the D-S5 evidence. Mark the
residual honestly: "new-build daemon-exit proven locally; ingress transport for
host.shutdown proven via the deployed endpoint enqueue; the two composed cover the path,
pixels-over-ingress pending FastPass."

### Task B (was E2, Okta-free half) — validate the admin screens' DATA CONTRACT live.
Can't screenshot the SPA, but prove every API call the pages make returns the shape the
components consume, against the DEPLOYED fork (bearer token from
`databricks auth token -p lakemeter`). App URL:
`https://omnigent-daveok-335310294452632.aws.databricksapps.com`. David is admin
(`/v1/me` → is_admin:true via the --admins roster).
- `GET /v1/hosts?all=true` → assert each row has host_id/name/owner/status/
  configured_harnesses/created_at/last_seen/session_count (the fleet fields HostsPage renders).
- Shares dialog contract: `GET /v1/hosts/{id}/permissions` (list), `PUT .../permissions/{user}`
  (add), `DELETE .../permissions/{user}` (revoke) — all against the SP-owned CoDA host
  (`host_e5955ca57bfaf3665cbae3be6e6856d0`), which David can manage as admin.
- Shutdown button contract: `POST /v1/hosts/{id}/shutdown` returns 200 `shutting_down`
  (already shown; re-assert).
This is "the screen's data is correct and its buttons hit working endpoints" — the
substantive half of D-S2, minus pixels.

### Task C — deepen live API-contract validation (hardens the real done-criteria).
Run these full loops against the deployed fork, bearer-token only:
- **Non-admin fail-closed:** `GET /v1/hosts?all=true` with a *non-admin* identity → 403.
  (No second identity handy without a colleague; can at least assert the code path via
  the fact that David-as-admin gets 200 and the 403 branch is unit-tested. Note the gap.)
- **grant → see → revoke → gone loop:** grant David `use` on the CoDA host →
  `GET /v1/hosts` shows it with permission_level "use", owned_by_current_user false →
  revoke → `GET /v1/hosts` no longer lists it. (Grant+browse already shown last session;
  add the revoke-removes-visibility half.)
- **view blocks launch / browse:** grant `view` (not `use`) → assert
  `GET /v1/hosts/{id}/filesystem` 403s and a launch is refused, while the host still
  appears in `GET /v1/hosts`. Proves the level gradation live.

### Task D — /review re-pass or /stress-test on the sharing model. Zero deps, pure analysis.

## Items that genuinely need a human (park until the user is back)
- **D-S2 pixels** — screenshot Hosts page / shutdown-confirm / Shares dialog. Needs FastPass.
- **D-S5 over-ingress** — new-build host over the Apps ingress. Needs FastPass (clear-binary
  route via CoDA terminal, OR throwaway `omnigent login`).
- **D-S4** — second human grantee. Needs a colleague to open the app.
- Clearing the 2 red PR checks (Maintainer Approval, E2E UI Required re-run) — repo-owner
  actions in the GitHub UI.

## Handy facts
- Deployed fork: `https://omnigent-daveok-335310294452632.aws.databricksapps.com`; token
  via `databricks auth token -p lakemeter --output json`.
- CoDA host: `host_e5955ca57bfaf3665cbae3be6e6856d0` (owner SP b213c03a…), still OLD build.
- David's laptop host (pid-451, OFF LIMITS): `host_f38301343733494fbedc831bade42233`.
- GitHub as dgokeeffe (EMU blocks the default identity): prefix API calls with
  `GH_TOKEN=$(gh auth token -u dgokeeffe -h github.com)`; git push works via the helper.
- Left-in-place demo state: David's `use` grant on the CoDA host; scratch dirs
  `/app/python/source_code/{omnigent-share-e2e,e5-cleanup}` in its sandbox.
