# Epic: Native-Session Credential Durability

> **For the implementer:** This is an epic split into workstreams and
> independently shippable tasks. It unifies three separately-reported bugs that
> share one root cause. Each task states its files, acceptance criteria, and a
> verification recipe. Findings were measured on a live CoDA box
> (`coda-daveok`, 2026-08-13) — see the Evidence appendix.

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

### B2. Audit every native harness for the same substitution

**Files:** `omnigent/inner/*_native_executor.py`, the per-harness bridge/config
writers, `omnigent/native_server_transport.py`.

Produce a table (credential → transport → expiring? → loopback alternative?)
covering pi, opencode, claude, codex, cursor, goose, hermes, kimi, kiro,
antigravity, qwen, copilot. This epic was found via two harnesses; the same
pattern is likely in others.

**Acceptance:** the table lands in this doc, and each row is either "loopback"
or has a ticket under Workstream C.

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

### C3. CoDA: re-run CLI auth configuration on every PAT rotation

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

## Evidence appendix (measured 2026-08-13, `coda-daveok`, 12 GiB container)

All four credential stores held **the same 820-char app-SP OAuth JWT shape**
(`iss=…/oidc`, `sub=f4b93eb4…` = the app SP), all long dead:

| Store | File mtime | `exp` | State |
|---|---|---|---|
| pi native session `c46d5958…` | 42.9 h ago | 2026-08-11 17:20:51Z | expired 41.9 h |
| opencode native session `afd8a17e…` (`xdg-config/opencode/opencode.json`, `xdg-data/opencode/auth.json`) | 42.1 h ago | — | expired 42.1 h |
| CoDA global opencode (`~/.local/share/opencode/auth.json`, `~/.config/opencode/opencode.json`) | 11.7 h ago | 2026-08-13 00:35:05Z | expired 10.7 h |
| `relayToken` (same `config.json`) | — | none | non-expiring, unaffected |

Meanwhile the pi TUI process was alive and healthy the entire time (started
Aug 11, still attached under tmux), which is exactly why the terminal kept
working while the chat did not.

### Reproduce the credential scan

```bash
python3 - <<'PY'
import json, base64, time, pathlib, re, glob
def exps(text):
    for tok in set(re.findall(r"eyJ[A-Za-z0-9_\-\.]{40,}", text)):
        p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
        try: e = json.loads(base64.urlsafe_b64decode(p)).get("exp")
        except Exception: e = None
        if e: yield e
home = pathlib.Path.home()
targets = [str(home/".omnigent/pi-native/*/config.json"),
           str(home/".omnigent/opencode-native/*/xdg-*/**/*.json"),
           str(home/".local/share/opencode/auth.json"),
           str(home/".config/opencode/opencode.json")]
for pattern in targets:
    for f in glob.glob(pattern, recursive=True):
        for e in exps(pathlib.Path(f).read_text()):
            age = (time.time() - e) / 3600
            print(f"{'EXPIRED %6.1fh' % age if age > 0 else 'valid  %6.1fh' % -age}  {f}")
PY
```

### Reproduce the silent-failure mechanism

```bash
cd ~/.omnigent/pi-native/*/ && python3 - <<'PY'
import json, pathlib, urllib.request
c = json.loads(pathlib.Path("config.json").read_text())
req = urllib.request.Request(f"{c['serverUrl']}/v1/sessions/{c['sessionId']}",
                             headers=dict(c["authHeaders"]))
with urllib.request.urlopen(req, timeout=20) as r:
    body = r.read(120)
# Expect 200 + text/html + "Databricks - Sign In": a success status for an auth failure.
print(r.status, r.headers.get("content-type"), body[:80])
PY
```

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
