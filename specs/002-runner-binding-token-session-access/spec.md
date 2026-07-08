# Feature Specification: Runner binding-token session access (header-mode)

**Feature Branch**: `002-runner-binding-token-session-access`

**Created**: 2026-07-08

**Status**: Draft

**Input**: Let a runner read ITS OWN bound session's spec/config using its tunnel
binding token as proof of identity, in ANY auth mode — so guest sessions on a
shared externally-owned host work even in header/proxy auth mode, where a
session-owner token cannot be minted.

## Context

This supersedes the header-mode-incompatible half of the prior feature
(`001-external-host-owner-mint`). That feature made the runner mint a
session-owner token — which only works in cookie-based (accounts/OIDC) auth
modes. The live CoDA deployment runs **header/proxy auth mode**, where no
server-side token can be minted, so the guest session's spec callbacks still
returned "not found" and the native terminal still failed to start. This
feature closes that gap with an auth-mode-independent mechanism, and is
strictly **narrower** in the authority it grants.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Guest session works on a shared host in header-mode (Priority: P1)

A user who is not the host owner launches a native coding-agent session on a
shared externally-owned host, on a deployment that authenticates users via a
trusted upstream proxy header (not a login cookie). The session starts and the
agent's terminal comes up.

**Why this priority**: This is the whole point — the shared-host workshop /
production pattern must work on the actual deployment, which is header-mode.
Without it, every guest session fails at launch there.

**Independent Test**: On a header-mode deployment, share an SP-owned host with a
guest, have the guest start a native session, and confirm it reaches a running
terminal (rather than failing at terminal start).

**Acceptance Scenarios**:

1. **Given** a header-mode deployment and a guest session launched on a shared
   host the guest doesn't own, **When** the session's runner resolves its
   agent spec/config from the server, **Then** those reads succeed and the
   session reaches a running terminal.
2. **Given** that same runner, **When** it presents its tunnel binding token as
   proof, **Then** the server grants it read access to **that one session only**
   — deriving the runner identity from the token and matching it against the
   session's recorded runner, with no reliance on any auth-mode secret.

---

### User Story 2 - The grant is tightly scoped (security) (Priority: P1)

The binding-token access must not become a general credential. It authorizes
read on exactly the one session the token's runner is bound to — nothing else.

**Why this priority**: Equal to US1 — a shared-host mechanism that leaked
broader access would be worse than the bug. This is the guardrail that makes
US1 safe to ship.

**Independent Test**: Present a binding token bound to session X while
requesting session Y (bound to a different runner) → denied. Present a valid
binding token against an operation requiring more than read → denied.

**Acceptance Scenarios**:

1. **Given** a binding token whose runner is bound to session X, **When** it is
   presented on a request for session Y, **Then** access is denied (as if the
   token were absent).
2. **Given** a valid binding token for session X, **When** it is presented on
   an operation requiring more than read access on X, **Then** it does not
   satisfy that requirement (it grants read, never write/manage/owner).
3. **Given** a malformed, empty, or unrecognized binding token, **When** it is
   presented, **Then** access resolves exactly as it does today (fail closed —
   the token grants nothing).

---

### User Story 3 - Existing access paths unchanged (Priority: P1, regression guard)

Normal user access (via login cookie or proxy header) and owner-on-own-host
sessions behave exactly as before.

**Why this priority**: The change touches the shared access-check helper on the
read path; it must not alter existing allow/deny outcomes for human callers.

**Independent Test**: A session owner reading their own session, a
non-owner-without-grant being denied, and an admin reading any session — all
unchanged.

**Acceptance Scenarios**:

1. **Given** a request with no binding token, **When** access is checked,
   **Then** the outcome is identical to today (owner allowed, admin allowed,
   unauthorized denied with the same status codes).

---

### Edge Cases

- **Token present but session has no recorded runner yet** (pre-launch) → the
  binding match fails → fall through to normal access checks (no crash, no
  grant).
- **Token valid but for a superseded runner** (session was relaunched with a
  new binding token) → the old token's derived runner no longer matches the
  session's current runner → denied. Only the current runner's token works.
- **Both a user identity AND a binding token present** → whichever grants access
  is sufficient; the binding token can only *add* read on its bound session, never
  remove or downgrade the user's own access.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The server MUST let a caller read a session's spec/config when the
  caller presents a runner tunnel binding token whose derived runner identity
  matches the runner recorded on that session.
- **FR-002**: The binding-token grant MUST be read-only and scoped to the single
  matched session; it MUST NOT yield a reusable user identity and MUST NOT
  satisfy any requirement above read (write/manage/owner).
- **FR-003**: The verification MUST NOT depend on any auth-mode-specific secret
  (no login cookie / signed token) — it MUST work identically in header/proxy
  mode and in cookie-based modes.
- **FR-004**: A missing, empty, malformed, or non-matching binding token MUST
  leave access resolution exactly as it is today (fail closed).
- **FR-005**: The runner MUST present its tunnel binding token on the callbacks
  it uses to resolve its own session's spec/config, so the server can perform
  the match.
- **FR-006**: The change MUST NOT alter allow/deny outcomes for callers who do
  not present a binding token (human users, admins, unauthorized callers).
- **FR-007**: When a session is relaunched with a new runner, only the current
  runner's binding token MUST grant access; a superseded token MUST NOT.

### Key Entities *(include if data involved)*

- **Runner binding token**: the per-launch secret the server issues and the host
  passes to the runner. Its derived runner identity is a pure function of the
  token (no secret needed to verify).
- **Session's recorded runner**: the runner identity the server stored on the
  session at launch. The match target.
- **Binding-token access grant**: a transient, read-only, single-session
  authorization derived from a token↔session match. Not an identity.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a header-mode deployment, 100% of guest native sessions on a
  shared host reach a running terminal (today: 0% — they fail at terminal
  start), and the runner's spec/config reads return success (today: not-found).
- **SC-002**: A binding token bound to session X grants access to X only;
  presenting it for any other session is denied. (Verifiable security test.)
- **SC-003**: A binding-token grant never satisfies a greater-than-read
  requirement. (Verifiable security test.)
- **SC-004**: Zero change in allow/deny outcomes for requests without a binding
  token, across owner / non-owner / admin / unauthorized. (Regression.)
- **SC-005**: After a session relaunch, the superseded binding token no longer
  grants access; the new one does.

## Assumptions

- The tunnel binding token is already a per-launch secret known only to the
  server (issuer) and the runner it was handed to; treating possession of it as
  proof of "I am that runner" matches how the tunnel handshake and the
  (cookie-mode) token-mint endpoint already trust it.
- The session already records the runner it was bound to at launch; the match
  target exists by the time the runner makes its spec callbacks.
- The runner reaches the server over its tunnel (not through the identity
  proxy), so it cannot forge the proxy identity header — which is exactly why a
  token-based proof is needed and appropriate here.
- The prior feature's launch-time signal (telling the runner it is serving a
  guest session) can be reused to decide when the runner attaches its binding
  token, keeping the change scoped to the guest-on-shared-host case.

## Out of Scope

- The session-owner token **mint** path (the cookie-mode mechanism from
  `001-external-host-owner-mint`) — reused where it applies, not extended here.
- Any change to how hosts are shared or how sessions are launched.
- Broadening the binding-token grant beyond read, or to more than the one bound
  session.
- Host-image / CoDA-side provisioning fixes already delivered separately.
