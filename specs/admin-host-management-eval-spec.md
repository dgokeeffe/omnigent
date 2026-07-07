# /speckit.specify — (D) Admin Host-Management: Verification & Evaluation Phase

**Component:** the `feat/admin-host-management` branch of the controlled fork (`dgokeeffe/omnigent`), deployed as `omnigent-daveok` on lakemeter.
**Feature:** Close every unexercised composition left after the build phase (spec C): browser-level proof of the admin screens, CoDA-side `share_and_launch`, a real second-human grantee, remote shutdown of a current-build host, CI, and a fresh-context risk review.
**Status:** Draft. Supersedes nothing — spec C is built and API-verified; this phase converts "proven via API/tests" into "proven in the exact production shape" and produces the PR evidence.
**Grounding:** verified state as of 2026-07-07: all C-S1..C-S5 criteria passed against the deployed fork **via REST + local component tests only**. No Playwright anywhere; the chrome-devtools walk stalled at an interactive Okta Verify prompt. CoDA connectivity was observed server-side (its host online in the fleet, grant→launch via curl), never from CoDA's own code path or a browser.

---

## 1. Problem

Every *component* of feature C is proven twice (live API + tests), but three full
chains have never run as single unbroken paths:

| # | Composition | Current evidence | Gap |
|---|-------------|------------------|-----|
| 1 | Browser → SPA admin screens → deployed server | vitest component tests only | No real browser has rendered the Hosts page, driven the shutdown confirm, or the Shares dialog against live data |
| 2 | CoDA app → `share_and_launch` → grant API | The API call CoDA makes was simulated with curl (200) | CoDA's own code constructing/issuing the call has not fired |
| 3 | Colleague → Apps SSO proxy → shared-host visibility → launch | David-as-grantee of an SP-owned host (valid but single human identity) | No second human `X-Forwarded-Email` has ever exercised the grant path |
| 4 | Server → Apps ingress → **new-build** daemon exit | Deployed endpoint 200 + old-build no-op (by design) observed remotely; full daemon-exit loop proven locally | The composed remote path needs a host running the new build |
| 5 | CI | ~380 server + ~180 web tests pass locally (Node-26 localStorage quirk masked some suites until flagged) | Branch never pushed; zero CI runs |
| 6 | Audit trail in production logs | Entrypoint `basicConfig(INFO)` confirmed in code; emission pinned by caplog tests | Nobody has grepped `/logz` for the real `audit:` lines today's session generated |

Additionally, two *judgment* items were deferred: the tenant-isolation stance
(`use` = full host-filesystem access — C-O3) is implemented but undocumented in
any reviewer-facing artifact, and no fresh-context review has read the ~2,000-line
auth-touching diff.

## 2. Users

| User | Need |
|------|------|
| **PR reviewer** (dgokeeffe fork, possibly upstream later) | CI signal, Demo screenshots/video, an explicit isolation-stance statement, a fresh-context review already applied |
| **Operator (David)** | Confidence the admin screens work in a real browser and that shutdown works against current-build remote hosts |
| **Colleague / grantee** | Proof their identity (via the Apps SSO proxy) sees and can launch on a shared host |

## 3. Requirements

### Verification — must produce observed evidence, not inference
- **D-R1 — Push + CI + PR.** Branch pushed to the `fork` remote; CI green (or failures triaged to pre-existing); PR opened against `dgokeeffe/omnigent` (NOT upstream) filling `.github/pull_request_template.md` — Summary, Test Plan, Demo (screenshots from D-R2), Type of change (UI/frontend box checked), Coverage notes.
- **D-R2 — Browser E2E of the three admin flows** against the deployed fork, via chrome-devtools MCP or Playwright: (a) Settings → Hosts renders the live fleet incl. the SP-owned CoDA host with status dots/harness pills/session counts; (b) Shut down action → confirm dialog → toast → row flips offline; (c) Shares dialog lists/adds/revokes a grant. Screenshots captured for the PR Demo section. *Prereq: one interactive SSO approval (see D-C1); with Playwright, persist `storageState` after that single login so reruns are non-interactive.*
- **D-R3 — CoDA-side `share_and_launch`.** Trigger the real call from the `coding-agents` app (or a freshly pointed CoDA app) against the fork; observe 2xx + the resulting grant/session server-side. No curl simulation.
- **D-R4 — Second-human grantee.** Grant a lakemeter colleague `use` on a host; they (their own browser/identity) see it labeled "Shared", and launch a session end-to-end. Screenshot or their confirmation recorded in the PR.
- **D-R5 — Remote shutdown of a new-build host.** A host daemon running THIS branch's build, connected through the Apps ingress, exits on `POST /v1/hosts/{id}/shutdown` and flips offline. Paths: upgrade the laptop's uv-tool daemon **only when no live session depends on it**, or an interactive `omnigent login` under an isolated `HOME` for a throwaway identity.
- **D-R6 — Audit lines observed in `/logz`** for at least one shutdown, one grant, one revoke (actor + target present).

### Evaluation — judgment outputs, not pass/fail
- **D-R7 — Fresh-context review.** Run `/review` (CCR) over the branch diff; triage every finding (fix / wontfix-with-reason). Optionally `/stress-test` on the sharing model.
- **D-R8 — Isolation stance documented.** The PR description states plainly: `use` grants browse of the entire host filesystem + launch as the host user; a grant governs who may *drive* the host, not what its ambient credentials *reach*. Include the open design question (launch-only `use` for shared hosts?) as a tracked follow-up, not silently.
- **D-R9 — Upstream posture.** Decide and record: fork-only vs. maintainer conversation first (the June channel-post draft), referencing C-O5.

## 4. Constraints
- **D-C1** Two user-gated unblockers sit in front of everything: `gh auth login -h github.com` as **dgokeeffe** (D-R1) and one interactive Okta Verify approval (D-R2, and D-R5's login path). Neither can be automated; both are single actions.
- **D-C2** The laptop's `omni host` daemon (old build) hosts live Claude Code sessions — never restart/shut it down from a session running through it.
- **D-C3** Old-build hosts no-op `host.shutdown` by design; a "shutdown didn't work" observation must first check the target's build.
- **D-C4** `coding-agents` runs an old CoDA/omnigent build; D-R3 may require redeploying it before its `share_and_launch` can be exercised.
- **D-C5** The `lakemeter:` target block in `deploy/databricks/databricks.yml` stays local-only — never push it.

## 5. Out of scope
- New feature work (grant granularity/groups C-O2, launch-only `use`, multi-replica shutdown routing) — record as follow-ups only.
- Upstream PR to `omnigent-ai/omnigent` (D-R9 decides *whether*, not *does*).
- Fleet-view query optimization (N+1 session counts) unless CI/scale evidence demands it.

## 6. Sequencing
```
[user] gh auth ──→ E1 push + CI + draft PR (D-R1)
[user] Okta OK ──→ E2 browser E2E + screenshots (D-R2) ──→ PR Demo filled
                   E3 colleague grantee (D-R4)  [needs a willing colleague]
                   E4 CoDA share_and_launch (D-R3)  [may need CoDA redeploy]
                   E5 new-build remote shutdown (D-R5)
                   E6 /logz audit grep (D-R6)
anytime:           E7 /review + isolation paragraph + upstream decision (D-R7..9)
```
E1/E7 are independent of the browser; E2 unlocks the PR's Demo section; E3–E5 can run in any order after their prereqs.

## 7. Success criteria
- **D-S1** CI green on the pushed branch; PR open with all template sections filled including real screenshots.
- **D-S2** All three admin flows observed in a real browser against the deployed fork (evidence attached to the PR).
- **D-S3** CoDA's own `share_and_launch` succeeded against the fork (server log/grant row as evidence).
- **D-S4** A second human identity saw a shared host and launched on it.
- **D-S5** A new-build remote host exited on shutdown through the Apps ingress.
- **D-S6** `/logz` shows actor+target audit lines for shutdown/grant/revoke.
- **D-S7** `/review` findings triaged to zero open; PR states the isolation stance; upstream-vs-fork decision recorded.
