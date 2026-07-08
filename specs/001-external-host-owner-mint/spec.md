# Feature Specification: External-host runners mint a session-scoped owner token

**Feature Branch**: `001-external-host-owner-mint`

**Created**: 2026-07-08

**Status**: Draft

**Input**: Enable an externally-owned Omnigent host (e.g. a Databricks App that
self-registers via `omnigent host`) to run sessions on behalf of *other* users,
by having its runner authenticate server callbacks with the session owner's
identity instead of the host owner's.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Guest runs a native session on a shared externally-owned host (Priority: P1)

A user who is *not* the host owner launches a native coding-agent session
(e.g. Claude Code) on a host that another principal owns and has shared with
them. The session starts, the agent's terminal comes up, and the agent can act
on the workspace — all under the guest's own identity.

**Why this priority**: This is the entire point of a shared host (workshops, a
team's always-on coding-agent app, a service-principal-owned host shared to
many people). Without it, every guest session fails at launch, making shared
externally-owned hosts unusable for anyone but the owner.

**Independent Test**: Share an externally-owned host with a second user, have
that user start a native session on it, and confirm the session reaches a live
terminal and completes a turn (rather than failing with a terminal-start
error). Fully delivers the shared-host value on its own.

**Acceptance Scenarios**:

1. **Given** an externally-owned host shared to a guest user, **When** the
   guest launches a native session on it, **Then** the runner resolves the
   session's agent spec and configuration successfully and the session reaches
   a running terminal (no `native_terminal_start_failed`, no fallback to a
   placeholder harness).
2. **Given** that same running session, **When** the runner acts on the
   server on the session's behalf, **Then** it does so with the *session
   owner's* (guest's) identity, so access decisions and audit attribution
   reflect the real end user, not the host owner.

---

### User Story 2 - Owner runs their own session on their own host is unchanged (Priority: P1)

A host owner launches a session on a host they own (the common case — e.g. a
laptop registered as a host running the owner's own session). Behavior is
exactly as it is today.

**Why this priority**: This is the dominant existing usage. The change must not
regress it, so it is equally critical — a fix that breaks own-host sessions
would be worse than the bug it fixes.

**Independent Test**: On a host whose owner matches the session owner, launch a
session and confirm it behaves identically to before the change (same
credential path, same success).

**Acceptance Scenarios**:

1. **Given** a host whose owner is the same principal as the session owner,
   **When** a session launches on it, **Then** the runner uses the same
   credential path it uses today (the inherited owner credential), with no
   change in behavior.

---

### Edge Cases

- **Older host daemon / older server (version skew)**: the guest-identity
  signal must be optional and default off, so a new server talking to an old
  host — or an old server talking to a new host — degrades to today's behavior
  without error, rather than breaking the launch.
- **Signal present but no session→owner binding resolvable**: if the runner is
  told to use the session identity but the server cannot resolve an owner for
  it, the runner must fail safe (behave as it does today, e.g. unauthenticated
  or inherited-credential requests) rather than crash the launch.
- **Guest lacks access to the host**: unchanged — a guest with no share on the
  host still cannot launch on it; this feature only governs *which identity*
  the runner acts as once a launch is legitimately authorized.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: When a host launches a runner for a session whose owner differs
  from the host owner, the runner MUST authenticate its server callbacks as the
  *session owner*, not the host owner.
- **FR-002**: When the session owner is the same principal as the host owner,
  the runner MUST keep using the existing (inherited owner credential) path,
  with no behavioral change.
- **FR-003**: The system MUST determine the "session owner vs host owner"
  distinction server-side (where both identities are known at launch) and
  convey the resulting choice to the runner; the runner MUST NOT be required to
  compute this distinction itself.
- **FR-004**: The guest-identity signal MUST be optional and backward
  compatible: a peer (host or server) that does not understand it MUST fall
  back to today's behavior without error.
- **FR-005**: A guest-identity session MUST successfully resolve its agent spec
  and session configuration from the server (the callbacks that fail today with
  a not-found response MUST succeed).
- **FR-006**: The runner MUST reuse the existing session-owner token issuance
  mechanism already used by server-provisioned sandboxes; no new token
  issuance, registration, or owner-resolution mechanism is introduced.
- **FR-007**: Failure to obtain a session-owner credential MUST degrade safely
  to today's behavior rather than aborting the launch.

### Key Entities *(include if feature involves data)*

- **Host owner**: the principal that owns the registered host (for an
  externally-owned app host, its service principal). Today it is the identity
  the runner acts as.
- **Session owner**: the user who created/owns the session (the guest, in the
  shared-host case). The identity the runner should act as under this feature.
- **Runner launch signal**: the per-launch instruction from server to host that
  determines which identity the runner should use for its callbacks. Optional,
  defaulting to today's behavior.
- **Session-owner credential**: a short-lived credential representing the
  session owner, obtained by the runner from the server using its per-launch
  binding token (the same mechanism server-provisioned sandboxes already use).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of native sessions launched by a guest on a shared
  externally-owned host reach a running terminal and complete at least one turn
  (today: 0% — they fail at terminal start).
- **SC-002**: Server callbacks that resolve a guest session's agent spec and
  configuration return success for the guest-launched case (today they return
  not-found for that case).
- **SC-003**: Zero regression for owner-on-own-host sessions — they succeed at
  the same rate as before the change.
- **SC-004**: Actions a guest session's runner takes on the server are
  attributed to the guest (session owner), verifiable in audit/attribution,
  not to the host owner.
- **SC-005**: A new-vs-old version-skew pairing (new server + old host, or old
  server + new host) launches sessions with no new failures relative to today.

## Assumptions

- The host has already been legitimately shared with the guest and the guest is
  authorized to launch on it; this feature governs *identity used at runtime*,
  not the share/authorization decision itself.
- The session-owner credential carries the full authority of the session owner
  (the same trust model server-provisioned sandboxes already use). Narrowing it
  to least-privilege (spec-read only) is explicitly out of scope (a separate
  follow-up).
- The server can resolve the session owner for a launched runner via the
  existing runner→session binding it already records at launch.
- This change is confined to the Omnigent platform (server, host daemon,
  runner). No change is required in the externally-owned host application
  itself beyond running a build that includes this change.

## Out of Scope

- Session-scoping / least-privilege narrowing of the minted credential.
- Any change to how hosts are shared or how launch authorization is decided.
- Unrelated operational noise (e.g. a stale-token rotation loop) and
  host-image provisioning fixes already handled elsewhere.
