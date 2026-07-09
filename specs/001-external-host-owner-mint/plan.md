# Implementation Plan: External-host runners mint a session-scoped owner token

**Branch**: `001-external-host-owner-mint` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-external-host-owner-mint/spec.md`

## Summary

When a host launches a runner for a session whose **owner differs from the host
owner** (the shared / externally-owned-host case, e.g. CoDA), the runner must
authenticate its server callbacks as the **session owner** rather than the host
owner. Today external-host runners always use the inherited host-owner
credential, so a guest session's spec callbacks (`GET
/v1/sessions/{id}/agent/contents`, `GET /v1/sessions/{id}`) 404 and the native
terminal fails to start.

**Technical approach**: the binding-token → owner-JWT mint that server-managed
sandboxes already use is *fully wired* for external hosts too — the server mints
+ binds the per-runner binding token on every launch, the host passes it into
the runner env, and the mint endpoint resolves the owner from the
runner→conversation binding. The only divergence is the runner's
credential-*preference order*. So the change is: the **server** (which knows
both owners at launch) sets an optional flag on the `host.launch_runner` frame
when `session_owner != host_owner`; the host threads it into the runner env; the
runner, when the flag is set, prefers the binding-token mint over the inherited
credential. Reuse `_make_managed_mint_factory`, the mint endpoint, and the owner
resolver — build nothing new.

## Technical Context

**Language/Version**: Python 3.10+ (repo targets 3.10+; runs on 3.12 in the CoDA container)

**Primary Dependencies**: FastAPI (server), httpx (runner callbacks), the existing Omnigent host/runner tunnel + frame codec (`omnigent/host/frames.py`)

**Storage**: N/A for this change (reuses existing runner→conversation binding in the conversation store; no schema change)

**Testing**: pytest (unit). Live end-to-end via the deployed `coding-agents` + `omnigent-daveok` apps on the `lakemeter` workspace, driven headlessly over the server API.

**Target Platform**: Linux (Databricks Apps container for CoDA; server on Databricks Apps). macOS/Linux for laptop hosts (unchanged path).

**Project Type**: Single project — server + host daemon + runner within the `omnigent` package.

**Performance Goals**: N/A (auth-selection change; the mint is already cached + re-minted near expiry by the existing factory).

**Constraints**: Wire-compatible with older hosts/servers (optional frame field, optional env var — version skew must degrade to today's behavior). No new long-lived credential at rest in the container.

**Scale/Scope**: 4 touched files, one boolean threaded through an existing launch chain. Targets the CoDA workshop (tens of concurrent guest sessions) and generalizes to any shared externally-owned host.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The project constitution (`.specify/memory/constitution.md`) is the unfilled
template (placeholder principles), so there are **no project-specific
governance gates to enforce**. General good-practice gates this plan meets:

- **Minimal surface / reuse over new code**: reuses the entire managed-mint
  mechanism; adds one flag + one selection branch. PASS.
- **No unjustified complexity**: no new stores, endpoints, or token types.
  Complexity Tracking table below is empty. PASS.
- **Backward compatibility**: optional frame field + optional env var default to
  today's behavior. PASS.

No violations to justify.

## Project Structure

### Documentation (this feature)

```text
specs/001-external-host-owner-mint/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output (entities: frame field, env var, credential)
├── quickstart.md        # Phase 1 output (headless E2E verification)
├── contracts/
│   └── launch-runner-frame.md   # the host.launch_runner frame + env-var contract
├── checklists/
│   └── requirements.md  # spec quality checklist (from /speckit-specify)
└── tasks.md             # Phase 2 output (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
omnigent/
├── host/
│   ├── frames.py            # HostLaunchRunnerFrame — add optional bool field
│   └── connect.py           # _handle_launch + _build_runner_env — thread flag → runner env
├── runner/
│   ├── identity.py          # define the new env-var name (beside RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR)
│   └── _entry.py            # _make_auth_token_factory — prefer mint when flag set
└── server/
    └── routes/
        └── sessions.py      # _launch_runner_on_host — set flag when session_owner != host_owner

tests/
└── (mirror existing host/runner/server test modules — see Phase 1 quickstart + tasks)
```

**Structure Decision**: Single-project layout; the change is confined to the
existing `omnigent/host`, `omnigent/runner`, and `omnigent/server` packages.
No new modules or directories. Tests extend the existing suites that already
cover these files.

## Complexity Tracking

> No constitution violations. No complexity to justify.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| (none)    | —          | —                                    |
