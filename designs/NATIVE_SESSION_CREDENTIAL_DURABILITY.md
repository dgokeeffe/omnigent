# Epic: Native-Session Credential Durability

> **For the implementer:** This is an epic split into workstreams and
> independently shippable tasks. It unifies three separately-reported bugs that
> share one root cause. Each task states its files, acceptance criteria, and a
> verification recipe. The original incident was measured on a live CoDA host;
> identifiers and credential-derived metadata are intentionally omitted.

## The one-line problem

**A credential with a ~1 hour lifetime is written into a file at session launch,
and only the runner WS tunnel ever refreshes it.** Every other consumer of that
file silently dies at T+1h. What the user sees depends only on *which path* the
dead credential sits on — the failure itself is identical.

## Reported symptoms, one cause

| Report | Actual mechanism | Blast radius |
|---|---|---|
| "pi sessions lose the responses in the chat but continue in the terminal and never come back" | `postEvent()` posts to `/v1/sessions/{id}/events` with the expired bearer from `config.json`; failure is unobservable and unretried | Chat goes blind. Terminal (WS tunnel, refreshes) stays healthy. pi keeps working, so the transcript diverges permanently |
| "opencode sessions lost connectivity after 1 hour, except they never came back — the session was dead" | The same class of expired bearer is baked into OpenCode's **provider/inference** config (`opencode.json` + `auth.json`), so model calls 401 | Whole session dead: the agent cannot call a model at all |
| (latent) policy / MCP tool calls | Use `relayUrl` + `relayToken` — loopback, non-expiring | Unaffected — this is the pattern to copy |

So pi and OpenCode are **the same bug in two places**, and the difference in
severity is purely which path the credential was on: event forwarding (cosmetic
but maddening) versus inference (fatal).

## Why it fails *silently* — the part that matters

Behind the Databricks Apps edge, an expired or absent bearer does **not** produce
a 401. Measured on this box against `/v1/sessions/{id}`:

```
stale bearer from config.json  -> HTTP 200, text/html, 57668 bytes, "<title>Databricks - Sign In</title>"
no auth header at all          -> HTTP 200, text/html, 57668 bytes, identical
```

Consequences for any client:

1. `fetch()` resolves, so a `catch` block never fires.
2. `resp.ok` is **`true`** — a status check cannot detect this either.
3. Only inspecting the response *body/content-type* reveals the failure.

`postEvent()` does none of the three, and its `catch` is documented as
deliberate: *"Keep Pi responsive even if Omnigent is temporarily unavailable."*
The intent is right; the effect is a permanent silent drop.

The runner WS tunnel already learned this lesson —
`runner/transports/ws_tunnel/serve.py` tracks `login_redirect_streak`, has
`_handle_refreshable_auth_failure()` for *"HTTP 302 login-page redirect"*,
invalidates the token, re-mints via `_refresh_auth_token()` at the top of every
reconnect, and escalates to a visible `RUNNER_TUNNEL_REJECTION` after a streak.
**The tunnel is the reference implementation; every other consumer is missing
one or more of its four properties: refresh, detect, retry, surface.**

## Epic goal

No native session can be silently degraded by credential expiry. Concretely:

- **G1** No expiring credential on a path that has a non-expiring loopback alternative.
- **G2** Every remaining edge-bound consumer refreshes on a timer tied to session lifetime, not to turn boundaries.
- **G3** Every consumer validates that a response came from Omnigent, not the edge's sign-in page.
- **G4** A failure that cannot be recovered is *visible* (status line + log + server-side signal) within one minute.
- **G5** Events that fail transiently are replayed, not dropped.

**Non-goals:** changing the Apps edge behaviour (we don't own it); lengthening
OAuth lifetimes (treats the symptom); reworking the tunnel (already correct).

The implementation graph is authoritative in Beads: `omnigent-qfw.1` detects
edge responses; `omnigent-qfw.2` removes Pi's avoidable edge transport;
`omnigent-qfw.3` refreshes OpenCode inference; `omnigent-qfw.4` refreshes CoDA
CLI configuration; `omnigent-qfw.6` adds replay; and `omnigent-qfw.7` owns the
shared harness contracts. Audit follow-ups are recorded in B2 below.

---

## Workstream A — Fail loudly (detection)

Cheapest work, unblocks diagnosis of everything else. Do this first even though
it fixes nothing by itself.

### A1. Reject edge sign-in responses instead of treating them as success

**Files:** `omnigent/resources/pi_native/omnigent_pi_native_extension.js`
(`postEvent`, `patchExternalSessionId`, and every `fetch` in the file).

Add one shared response validator: a response is acceptable only if the status
is 2xx **and** `content-type` is JSON. Anything else — notably `text/html` —
is an auth/edge failure, not a success.

**Acceptance:** a unit test in `omnigent_pi_native_extension.test.js` stubs
`fetch` to return `200 text/html` with a sign-in body and asserts the caller
treats it as failure (retries or surfaces, never silently returns).

### A2. Surface a degraded state on the pi status line

**Files:** same extension, `setOmnigentStatus()` (already writes the pi status
line and is already called for other states).

On N consecutive event-post failures, set a `· chat offline` state; clear it on
the first success.

**Acceptance:** with a stubbed always-failing `fetch`, the status string
contains the degraded marker; after a success it does not.

### A3. Log and count drops where an operator will see them

**Files:** extension (stderr/log via existing plumbing), plus a server-side
counter if one is cheap.

**Acceptance:** a dropped event produces exactly one log line naming the
session, the reason (`edge-signin` / `transport` / `http-5xx`), and the count of
consecutive failures. No per-event spam.

## Workstream B — Remove expiry where a loopback path exists (G1)

### B1. Route pi events through the tool relay

**Files:** extension (`postEvent` → use `relayCredentials()` like
`evalNativePolicyHttp` does), and whatever writes `relayUrl`/`relayToken` into
`config.json`.

`relayToken` is **already in the same `config.json`** and is already used by the
policy/MCP path. It is loopback and non-expiring, so it never meets the edge and
cannot hit the sign-in page. This single change would have prevented the pi
symptom entirely.

**Acceptance:** with `authHeaders` deliberately set to an expired bearer and the
relay configured, events still reach the server. Test asserts `postEvent`
targets `relayUrl` when relay credentials exist, and only falls back to
`serverUrl` when they don't.

**Risk to check:** confirm the relay accepts (or can be extended to accept) the
events route, and that the relay's own lifetime covers the whole session.

### B2. Native credential-transport audit

Audit snapshot: Omnigent `dev` at `8da320a16da619e2de7ed75e5b83ab399a4b9746`;
CoDA private `dev` at `efaf919f6e2c36286bc34419bbea5c8c7bd5e9e1`.
This table is the authority for the current code, not a claim about an earlier
incident image. “Terminal-visible” means readable by the harness process and its
same-user descendants; all native CLIs necessarily see their own inference
credential. A vendor/user-owned credential required by its own CLI is safe
within this epic when the vendor owns refresh; an Omnigent-minted or copied
expiring credential is unsafe when a narrower loopback or live-helper path is
available.

| Harness | Credential consumer and location | Purpose | Transport | Source and lifetime | Terminal visibility | Refresh owner and trigger | Sign-in HTML detection | Retry / idempotency | Deterministic evidence | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Pi | Pi provider in per-session `models.json`; extension `config.json` `authHeaders`; relay token in the same bridge config | Inference; Omnigent events/policy/MCP | Inference and fallback callbacks cross the edge; relay is loopback | Provider `apiKey` is an auth command evaluated per request; `authHeaders` is an Apps bearer (about 1 h); relay token is session-lived | Provider helper and bridge config are readable by the Pi process; no SP client secret | Pi re-runs the provider helper per request; `PiNativeExecutor._refresh_auth_headers()` re-mints only on a web turn; relay is runner-owned | Extension did not validate HTML at this snapshot | Event POST was best-effort/no durable replay; relay calls are request/response | `tests/test_pi_native_credentials.py`, `tests/inner/test_pi_native_executor.py`, `omnigent/resources/pi_native/omnigent_pi_native_extension.test.js` | **Unsafe, owned:** `omnigent-qfw.2` owns removal of the avoidable edge credential; detection, replay, and shared tests are dependency-layer work rather than additional owners for this transport row |
| OpenCode | Per-session `xdg-config/opencode/opencode.json` and `xdg-data/opencode/auth.json`; `opencode serve` loopback password | Inference; native-server control and event forwarding | Inference crosses provider/Databricks edge; runner-to-OpenCode control is loopback basic auth | User `auth.json` is copied at spawn; managed gateway bearer is snapshotted at launch; server password is process/session-lived | Inference credential is visible to the OpenCode process in its isolated data home; Omnigent server bearer is not required for loopback control | OpenCode owns refresh for its native login; Omnigent does not refresh the copied/managed bearer after spawn | Provider failure becomes an OpenCode auth error, not direct validation of Databricks `200 text/html` | SSE reconnects and stable event keys dedupe in-process, but inference auth failure is fatal | `tests/test_opencode_native_bridge.py::test_seed_opencode_auth_copies_user_auth`, `tests/test_opencode_native_forwarder.py`, `tests/test_opencode_native_app_server.py` | **Unsafe, owned:** `omnigent-qfw.3` owns the snapshotted inference credential; shared contract coverage is dependency-layer work rather than another owner for this transport row |
| Claude | Claude Code `apiKeyHelper` in per-session settings; optional Bedrock token in `AWS_BEARER_TOKEN_BEDROCK`; loopback relay config | Inference; policy/MCP/events | Inference crosses the configured provider edge; relay is loopback | Gateway helper is invoked on Claude Code’s TTL and can mint fresh tokens; Bedrock `auth_command` is resolved once at launch | Helper command or Bedrock token is visible only to the Claude process/session tree; raw `ANTHROPIC_API_KEY` is explicitly removed on helper path | Claude Code re-invokes `apiKeyHelper` on `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`; Bedrock env has no refresh hook | Omnigent hook reauth handles 401/403 and login redirects, but provider HTML is provider-dependent | Hook retry is bounded once; relay is request/response; provider turn retry belongs to Claude | `tests/test_claude_native.py` helper/TTL and Bedrock cases; `tests/test_native_policy_hook.py`; `tests/test_claude_native_bridge.py` | **Unsafe, owned:** `omnigent-qfw.10` covers the expiring Bedrock `auth_command` launch snapshot |
| Codex | Per-session `CODEX_HOME/config.toml` `model_provider.auth.command`, or native `auth.json`; loopback app-server and relay secrets | Inference; app-server control; policy/MCP/events | Inference crosses provider edge; app-server/relay are loopback | Databricks/provider auth command is executed by Codex when needed; subscription auth lifetime is Codex-owned | Codex process can invoke its helper/read its own auth; raw ambient `OPENAI_API_KEY` is stripped from managed launch | Codex owns `auth.command` invocation and subscription refresh | Omnigent policy hook recognizes edge auth failures; inference handling is Codex-owned | App-server control uses stable local session/thread IDs; hook retry is bounded | `tests/inner/test_databricks_executor.py::test_codex_executor_uses_cli_auth_command_not_env_token`, `tests/test_codex_native_app_server.py`, `tests/test_native_policy_hook.py` | **Safe:** refresh-capable helper plus loopback control; include in `omnigent-qfw.7` |
| Cursor | Cursor agent/SDK using real `$HOME/.cursor` login or `CURSOR_API_KEY`; loopback bridge/relay | Vendor inference; Omnigent events/policy/MCP | Vendor edge for inference; loopback for Omnigent bridge | Cursor-owned login or explicit/stored API key; no Omnigent Apps bearer is used for inference | Cursor necessarily sees its own login/key; runner spawn env sends only a Cursor credential selected by the user/spec | Cursor owns login refresh; API-key lifetime is operator-owned | Not applicable to Omnigent Apps edge on inference; forwarder failures are surfaced separately | Forwarder supervision reconnects; native IDs scope mirrored items | `tests/runtime/test_cursor_spawn_env.py`, `tests/test_cursor_native_forwarder.py`, `tests/test_cursor_native_bridge.py` | **Safe within this epic:** own-auth vendor edge plus loopback Omnigent control; include in `omnigent-qfw.7` |
| Goose | Goose’s real config (normally `~/.config/goose/config.yaml`); loopback bridge/relay | Vendor/provider inference; Omnigent events/policy/MCP | Provider edge chosen by Goose; loopback for Omnigent control | Goose-owned provider config; Omnigent deliberately injects no gateway credential | Goose reads its own config as the terminal user; no Omnigent Apps bearer is added | Goose/provider owns refresh | Not applicable to an Omnigent Apps bearer unless the user independently configured that provider | Forwarder reconnect supervision; provider retry belongs to Goose | `tests/onboarding/test_goose_auth.py`, `tests/runtime/test_provider_spawn_env.py` (no Goose gateway env), `tests/test_goose_native_forwarder.py` | **Safe within this epic:** own-auth CLI and no avoidable Omnigent edge bearer; include in `omnigent-qfw.7` |
| Hermes | User `~/.hermes/config.yaml` and `auth.json`, copied to per-session `HERMES_HOME`; loopback bridge/relay | Provider inference; Omnigent events/policy/MCP | Provider edge chosen by Hermes; loopback for Omnigent control | Auth/config snapshot is copied at launch and contains Hermes-owned credential material | Hermes process reads its isolated copy; no Omnigent Apps bearer is injected for inference | Hermes owns refresh within the copied home; a later external re-login is picked up only on respawn | Not applicable to an Omnigent Apps bearer unless user-configured | Forwarder reconnect supervision; provider retry belongs to Hermes | `tests/test_hermes_native_bridge.py`, `tests/inner/test_hermes_native_executor.py`, `tests/test_hermes_native_forwarder.py` | **Safe within this epic:** copy includes the CLI’s own refresh state, not an Omnigent launch bearer; include in `omnigent-qfw.7` |
| Kimi | Session `KIMI_CODE_HOME`; symlinks user `oauth/` and `credentials/`; hook config contains Omnigent callback coordinates | Vendor inference; policy/events/MCP | Kimi vendor edge; hook/relay path is loopback or authenticated server callback | Kimi login state remains linked to the user home, so rotations written there remain visible; relay token is session-lived | Kimi sees its own login; hook command line contains no secret | Kimi owns OAuth refresh; symlinks make refreshed files visible without respawn | Not applicable to Omnigent Apps inference; callback contract joins `omnigent-qfw.7` | Forwarder retries a failed line without advancing its cursor; supervisor uses bounded backoff | `tests/test_kimi_native_credentials.py`, `tests/test_kimi_native_forwarder.py`, `tests/test_kimi_native_bridge_hook_config.py` | **Safe within this epic:** live-linked own-auth state and cursor-preserving retry; the CLI can write through to its real credential store as required for vendor refresh, so operator-owned backup/recovery remains outside this transport epic; include in `omnigent-qfw.7` |
| Kiro | Kiro CLI’s own login under its real user state; per-session MCP bridge config | Vendor inference; Omnigent policy/events/MCP | Kiro vendor edge; Omnigent relay is loopback | Kiro-owned login; ambient provider/cloud credentials are stripped from child env; relay token is session-lived | Kiro sees only its own login and explicitly allowed environment; Omnigent relay token stays in hardened bridge state | Kiro owns login refresh; runner owns relay lifetime | Not applicable to Omnigent Apps inference | Session forwarder and permission mirror have bounded retries/timeouts | `tests/test_kiro_native_bridge.py`, `tests/test_kiro_native_session_forwarder.py`, `tests/inner/test_kiro_native_executor.py` | **Safe within this epic:** own-auth vendor edge and explicit child allowlist (`inherit_env=False`) plus loopback relay; include in `omnigent-qfw.7` |
| Antigravity | Agy OAuth in OS keyring/real HOME; file credential markers copied into isolated `--gemini_dir`; per-session relay config | Vendor inference; Omnigent events/policy/MCP | Vendor edge for inference; loopback relay; CLI remote attach may use the Omnigent edge | OAuth/keyring is agy-owned; isolated marker copy is session-scoped; relay token is session-lived; CLI reader bearer is an attach-time snapshot | Agy sees its own OAuth; ambient unrelated credentials are not required; the reader receives only the Omnigent bearer, never its minting secret | Agy owns OAuth refresh; runner clients can use refresh-capable auth, but the CLI-side reader has none and only a reconnect refreshes attach headers | No direct Databricks inference path; CLI-side remote reader does not detect sign-in HTML | Reader/forwarder supervision reconnects; bridge IDs prevent cross-session reuse, but an already-running CLI reader cannot recover its static auth | `tests/test_antigravity_native_bridge.py`, `tests/test_antigravity_native_launch.py`, `tests/test_antigravity_native_reader.py` | **Unsafe, owned:** `omnigent-qfw.12` gives the CLI remote reader refresh/detection or constrains it to loopback |
| Qwen | Qwen process `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`, or user `~/.qwen` auth; loopback bridge/relay | Provider inference; Omnigent events/policy/MCP | Provider edge for inference; loopback for Omnigent control | Managed gateway `auth_command` is executed once at process start and exported as a token; user auth is Qwen-owned | Qwen process sees the concrete gateway token in env | No Omnigent refresh after spawn; Qwen owns only its interactive/user auth | No Omnigent validation of provider `200 text/html` | Forwarder supervision retries callback transport; an expired inference token ends model calls | `tests/inner/test_qwen_executor.py`, `tests/runtime/test_provider_spawn_env.py`, `tests/test_qwen_native_forwarder.py` | **Unsafe, owned:** `omnigent-qfw.11` keeps Qwen gateway auth live for the process lifetime |
| Copilot | Copilot SDK `github_token`, resolved from spec, dedicated secret ref, or ambient GitHub token; SDK subprocess | GitHub Copilot inference/tools | GitHub edge only; no Databricks AI gateway support | User/operator GitHub token resolved when executor/session starts; token lifetime is GitHub/operator-owned | Copilot SDK/CLI receives the token; it is not written to workspace files by this path | Operator/SDK owns rotation; a new executor session re-resolves stored/ambient auth | Not applicable to Omnigent Apps edge | SDK returns retryable turn errors; session recreation occurs when fixed session inputs change | `tests/onboarding/test_copilot_auth.py`, `tests/runtime/test_provider_spawn_env.py`, `tests/inner/test_copilot_executor.py` | **Safe within this epic:** explicit own-auth vendor credential, not an Omnigent edge bearer; include in `omnigent-qfw.7` |

#### Follow-ups discovered by the audit

The audit created these non-duplicative follow-up Beads from conclusive static
source and test evidence:

- **`omnigent-qfw.10` — P1: “Refresh Claude-native Bedrock auth-command credentials or reject expiring sources.”**
  **Code path:** `omnigent/claude_native.py::_bedrock_config_for_native_claude`.
  **Invariant:** an edge-bound inference credential must remain valid for the
  native-session lifetime or fail visibly before launch. **Static evidence:**
  the function executes a provider `auth_command` once, exports the result as
  `AWS_BEARER_TOKEN_BEDROCK`, and records that Bedrock ignores `apiKeyHelper`;
  `tests/test_claude_native.py::test_bedrock_config_for_native_claude_resolves_auth_command`
  proves the launch snapshot. **Scope:** add a refresh-capable delivery
  mechanism, or reject/document expiring command output. **Dependencies:**
  child of `omnigent-qfw`; blocks `omnigent-qfw.7` and therefore
  `omnigent-qfw.9`; it does not duplicate `omnigent-qfw.3`, which is OpenCode
  only. Runtime reproduction is unnecessary and would require waiting for or
  using a real expiring bearer; the one-shot subprocess and env assignment are
  conclusive deterministic source/test evidence.
- **`omnigent-qfw.11` — P0: “Keep Qwen-native gateway credentials live for the process lifetime.”**
  **Code path:**
  `omnigent/inner/qwen_executor.py::QwenExecutor._resolve_gateway_env`.
  **Invariant:** managed gateway inference must not outlive a snapshotted bearer.
  **Static evidence:** the function executes
  `HARNESS_QWEN_GATEWAY_AUTH_COMMAND` once and exports the result as
  `OPENAI_API_KEY`; its docstring explicitly says restart is the only refresh.
  **Scope:** prefer a per-request helper/loopback shim; otherwise rotate safely
  without exposing the bearer. **Dependencies:** child of `omnigent-qfw`;
  blocks `omnigent-qfw.7` and `omnigent-qfw.9`; distinct from OpenCode issue
  `omnigent-qfw.3`. Runtime reproduction is unnecessary and would expose a
  real bearer for no additional proof; the launch-only assignment establishes
  the lifetime mismatch deterministically.
- **`omnigent-qfw.12` — P1: “Refresh or loopback-scope Antigravity CLI reader callbacks.”**
  **Code path:** `omnigent/antigravity_native.py::_attach_cli` →
  `omnigent/antigravity_native_reader.py::run_reader_with_bridge`.
  **Invariant:** a long-running authenticated callback client must refresh and
  validate the expected response contract, or use a session-lived loopback
  capability. **Static evidence:** `_attach_cli` passes `auth=None`; both
  functions document that remote headers are a static bearer for the reader's
  lifetime and only a new attach refreshes them. **Scope:** thread a
  refresh-capable auth flow into the CLI reader or reject remote-edge reader
  mode, and add sign-in-HTML/fake-expiry coverage. **Dependencies:** child of
  `omnigent-qfw`; blocks `omnigent-qfw.7` and `omnigent-qfw.9`; it does not
  duplicate Pi callback issue `omnigent-qfw.2`. Runtime reproduction would
  require a live remote session and expiring bearer, while the explicit
  `auth=None` data flow is conclusive.

#### Reconciliation with the CoDA diagnosis

The private CoDA artifact was verified at
`efaf919f6e2c36286bc34419bbea5c8c7bd5e9e1`:
`docs/plans/2026-08-13-session-admission-and-secret-boundary-findings.md`.
Its measured identifiers and credential values are intentionally not repeated
here.

| CoDA finding | Audit result and owner |
| --- | --- |
| Memory admission counted reclaimable page cache | Fixed by CoDA commit `c801679` with deterministic capacity tests; `omnigent-qfw.5.1` owns non-implementation ancestry/regression verification. Functional rather than a credential transport. |
| Capacity telemetry and `/proc` parsing | Observability-only follow-ups outside this epic; no bearer or secret crosses a boundary. |
| App resource binding / deploy provenance | Resource binding was corrected; deploy provenance is a release-process concern, not a native-harness credential transport. Do not treat Workspace sync or container edits as deploy sources. |
| Browser-terminal secret boundary | **Unsafe, owned by `omnigent-b0y` (P0).** The terminal environment builder is deny-list based, so newly bound credential-shaped variables can reach model-controlled terminals. Exact private-repository code evidence is retained in the owning Bead rather than published here. |
| Hand-written `[DEFAULT]` PAT and silent sync failure | The rotator-to-agent-config stale-token defect is owned by `omnigent-qfw.4`. Emergency raw PAT insertion and sync-health visibility remain operational/process follow-ups and must not be presented as a supported refresh path. |
| CoDA global agent configs not updated after PAT rotation | **Owned:** `omnigent-qfw.4`; it must update all configured CLI consumers on every rotation without exposing the PAT to terminal env. |

Required additional Bead:

- **`omnigent-b0y` — P0: “Make CoDA browser-terminal environment secret-deny-by-default.”**
  The private CoDA terminal environment builder uses copy-then-subtract rather
  than an explicit allowlist. Exact code paths, test evidence, affected
  credential classes, priority, dependencies, and the reason runtime
  reproduction is inappropriate are retained in the owning Bead. The audit
  records only the durable invariant: model-controlled terminals inherit
  approved non-secret entries, never ambient credential-shaped variables.
  `omnigent-qfw.9` depends on `omnigent-b0y`, so the final live/security gate
  cannot run before this private CoDA boundary is fixed.

**B2 completion rule:** each unsafe harness row maps to one implementation
owner, while dependency-layer detection/replay/contract tasks remain separately
linked in the Beads graph. CoDA-only findings map to exactly one CoDA follow-up.
No live deployment or credential value is needed to establish these static
transport findings.

## Workstream C — Refresh on a timer, not per turn (G2)

### C1. Keep `config.json` `authHeaders` fresh for the session's lifetime

**Files:** `omnigent/inner/pi_native_executor.py`,
`omnigent/pi_native_bridge.py` (`merge_auth_headers` already exists — the writer
is there, only the *schedule* is missing).

Today the re-mint is executor-driven **per runner turn**, so a session driven
from the TUI never triggers one. Add a session-lifetime keepalive that re-mints
well inside the credential's lifetime (e.g. every 15 min for a 1 h token).

**Acceptance:** a session that takes **no** chat-driven turns for > 1 h still has
a non-expired bearer in `config.json`; test drives a fake clock and asserts the
writer was called.

### C2. Refresh OpenCode's provider credential (the fatal case)

**Files:** `omnigent/inner/opencode_native_executor.py`,
`omnigent/opencode_native_app_server.py`, plus per-session
`xdg-config/opencode/opencode.json` and `xdg-data/opencode/auth.json` writers.

The bearer is baked into the provider config at launch, so inference 401s at
T+1h and the session is dead with no recovery path. Either refresh the file on a
timer (C1's mechanism) or point OpenCode at a loopback shim that mints per
request (preferred — see `claude_gateway_shim.py` for prior art).

**Acceptance:** an OpenCode native session still completes a model call after
its original bearer's `exp` has passed.

### C3. CoDA: re-run CLI auth configuration on every PAT rotation (`omnigent-qfw.4`)

**Files (CoDA repo):** `app.py` `_configure_all_cli_auth()`, `pat_rotator.py`.

`_configure_all_cli_auth()` is called **only** from `_bootstrap_pat()`, so the
global agent CLI configs are written once at bootstrap and never refreshed,
even though the rotator mints a new token every `PAT_TOKEN_LIFETIME` (default
900 s). Measured: `~/.local/share/opencode/auth.json` held a bearer that expired
10.7 h earlier.

**Acceptance:** after a rotation, the agent CLI config files' credentials match
the rotator's current token.

## Workstream D — Durability and reconciliation (G5)

### D1. Disk-backed event queue with replay

**Files:** extension + `omnigent/pi_native_bridge.py`.

The bridge dir already has an `inbox` pattern with lexicographic ordering and a
sequence tiebreaker — reuse it for an *outbox*. Drain with exponential backoff.
The extension already dedupes by message fingerprint, so replay is safe.

**Acceptance:** kill the server, run a turn, restart the server → the chat shows
the turn without duplicates.

### D2. Reconcile from the pi transcript on reconnect

**Files:** `omnigent/pi_native_resume.py` (resume machinery exists), server
events route.

Even with D1, a session that was dropped for hours should be able to catch up
from pi's own session file — the ground truth the terminal was happily using the
whole time.

**Acceptance:** a session whose events were dropped for > 1 h renders completely
in chat after a reconnect.

## Workstream E — Regression tests and tooling

### E1. Edge-simulator fixture

A shared test double that answers `200 text/html` with a sign-in body, so every
harness's client can be tested against the real failure mode rather than a 401
that never happens in production.

### E2. Per-harness "survives credential expiry" contract test

Parameterised over native harnesses: advance past `exp`, assert the harness
still forwards events / still calls a model / surfaces a visible error — never
silently degrades.

### E3. `omnigent doctor` credential-expiry check

Scan the live session config files, decode `exp`, and report anything expired or
expiring soon. This epic was diagnosed with a throwaway version of exactly this
(see Evidence) — make it a first-class command so the next incident is a
one-liner.

**Acceptance:** on the box in the Evidence appendix, the command flags the pi and
OpenCode configs as expired, with ages.

---

## Suggested order

| Order | Task | Why here |
|---|---|---|
| 1 | A1, A2 | Makes every later fix verifiable; turns a silent bug into a visible one |
| 2 | B1 | Smallest change that fully fixes the reported pi symptom |
| 3 | C2 | The fatal one (dead OpenCode sessions) |
| 4 | C1, C3 | Closes the remaining edge-bound paths |
| 5 | E1, E2 | Locks it all down before the pattern reappears |
| 6 | D1, D2, B2, A3, E3 | Durability, audit, and tooling |

## Evidence appendix

The incident established three durable facts without preserving host,
workspace, session, credential, or exact-expiry identifiers in this design:

1. Pi and OpenCode session credential snapshots outlived their bearer lifetime.
2. The Pi TUI remained healthy while authenticated chat callbacks stopped.
3. A session-lived loopback relay capability remained valid and is the safer
   transport pattern.

Deterministic regression tests use synthetic credentials, fake clocks, and a
local edge simulator. Operators must not replay live bearer material or print
credential-store contents to reproduce this contract.

## Open questions

1. Does the tool relay's lifetime always cover the full session, or can it die
   independently? B1 assumes it outlives the bearer.
2. Can the relay serve the events route as-is, or does B1 need a server-side
   addition?
3. Is there any consumer that *must* use an edge-bound bearer (no loopback
   option)? Those are the only ones that need C1's timer.
4. Should the server treat "no events for N minutes from a session whose runner
   is alive" as a health signal it can surface, rather than relying on clients
   to self-report?

## Related

- CoDA-side findings from the same session, including the memory-admission bug
  and the terminal secret-boundary gaps:
  `coda:docs/plans/2026-08-13-session-admission-and-secret-boundary-findings.md`
  (Finding 3b's "deploy provenance" note also explains why an in-container
  hotfix to any of the above would silently disappear).
- `runner/transports/ws_tunnel/serve.py` — the reference implementation for
  refresh + detect + retry + surface.
