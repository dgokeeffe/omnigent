---
description: "Task list for External-host runners mint a session-scoped owner token"
---

# Tasks: External-host runners mint a session-scoped owner token

**Input**: Design documents from `specs/001-external-host-owner-mint/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED. The spec's quickstart defines explicit unit-test
checkpoints and this is a security-sensitive auth-selection change, so tests are
generated (TDD-leaning: write the failing test, then the code).

**Organization**: The feature is a single credential-flow change threaded
server → frame → host → runner. US1 (guest session works) is delivered by the
whole chain; US2 (owner-on-own-host unchanged) is a regression guard satisfied
by the *conditional* nature of the change and its own tests. Tasks are ordered
along the data flow; both stories are validated in Phase 6.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no incomplete deps)
- **[Story]**: US1 (guest works) / US2 (owner unchanged); shared-infra tasks unlabeled

## Path Conventions

Single project. All paths under `omnigent/` (source) and `tests/` (repo root).

---

## Phase 1: Setup

**Purpose**: Locate the exact edit sites + existing test modules to extend.

- [X] T001 Confirm the four edit sites resolve at current HEAD (line drift): `HostLaunchRunnerFrame` in `omnigent/host/frames.py`, `_launch_runner_on_host` in `omnigent/server/routes/sessions.py`, `_build_runner_env` + `_handle_launch` in `omnigent/host/connect.py`, `_make_auth_token_factory` in `omnigent/runner/_entry.py`, and the env-var constant block in `omnigent/runner/identity.py`.
- [X] T002 Identify the existing test modules to extend under `tests/` for: frame encode/decode, `_launch_runner_on_host`, `_build_runner_env`, and `_make_auth_token_factory` (grep for each symbol under `tests/`).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The wire/env contract carriers everything else depends on. MUST land before server/host/runner wiring.

- [X] T003 Add optional field `prefer_binding_token_mint: bool = False` to `HostLaunchRunnerFrame` in `omnigent/host/frames.py` (mirror the existing optional `harness` field; update the docstring).
- [X] T004 Define the runner env-var constant (e.g. `RUNNER_PREFER_BINDING_TOKEN_MINT_ENV_VAR = "OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT"`) in `omnigent/runner/identity.py`, beside `RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR`.

**Checkpoint**: frame + env-var name exist and import cleanly.

---

## Phase 3: US1 — Guest session works on a shared externally-owned host (Priority: P1)

**Goal**: A session whose owner ≠ host owner runs with the session-owner
identity, so spec callbacks 200 and the terminal starts.

**Independent test**: create a claude-native session as a non-host-owner on the
SP-owned `coding-agents` host; it reaches a running terminal (quickstart.md).

### Tests for US1 (write first, expect fail)

- [X] T005 [P] [US1] Test: `HostLaunchRunnerFrame` encode/decode round-trips `prefer_binding_token_mint=True`, and decoding a frame WITHOUT the field yields `False` (wire compat). In the frame test module from T002.
- [X] T006 [P] [US1] Test: `_launch_runner_on_host` sets `prefer_binding_token_mint=True` on the frame when the session owner ≠ host owner. In the sessions test module.
- [X] T007 [P] [US1] Test: `_build_runner_env` sets `OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT="1"` when the flag is True. In the connect test module.
- [X] T008 [P] [US1] Test: `_make_auth_token_factory` returns the binding-token mint factory when the env flag is set AND a binding token is present, EVEN IF an inherited credential would resolve. In the `_entry` test module.

### Implementation for US1

- [X] T009 [US1] In `_launch_runner_on_host` (`omnigent/server/routes/sessions.py`), resolve session owner (`get_session_owner`/`_get_session_owner_id` on `conv`) and host owner (from `host_conn`/host record), set `prefer_binding_token_mint = session_owner != host_owner` on the `HostLaunchRunnerFrame`.
- [X] T010 [US1] In `_handle_launch` (`omnigent/host/connect.py`), pass `prefer_binding_token_mint=frame.prefer_binding_token_mint` into `_build_runner_env`; in `_build_runner_env`, when True, set the env var from T004 (`"1"`). Add the keyword param.
- [X] T011 [US1] In `_make_auth_token_factory` (`omnigent/runner/_entry.py`), before the user-credential probe: if the T004 env flag is set AND `_runner_tunnel_binding_token_from_env()` returns a token, return `_make_managed_mint_factory(resolved_server_url, binding_token)`. Fall through to today's order otherwise (incl. flag set but no binding token → safe degrade).

### Safe-degrade coverage for US1 (FR-007)

- [X] T021 [P] [US1] Test: `_make_auth_token_factory` with the env flag SET but NO binding token present (`_runner_tunnel_binding_token_from_env()` → None) falls through to today's credential order and does NOT raise — safe degrade (FR-007). In the `_entry` test module.

**Checkpoint**: US1 tests (T005–T008, T021) pass. Guest launch resolves spec (200) and reaches a terminal.

---

## Phase 4: US2 — Owner-on-own-host unchanged (Priority: P1, regression guard)

**Goal**: When session owner == host owner, nothing changes.

**Independent test**: a session created by the host owner on their own host uses
the inherited-credential path exactly as before.

### Tests for US2

- [X] T012 [P] [US2] Test: `_launch_runner_on_host` sets `prefer_binding_token_mint=False` when session owner == host owner. In the sessions test module.
- [X] T013 [P] [US2] Test: `_make_auth_token_factory` with the env flag UNSET returns the inherited-credential factory first (today's order), even when a binding token is present. In the `_entry` test module.

**Checkpoint**: T012–T013 pass; no behavioral change for the owner path (US2 is satisfied by the conditional in T009 + the flag-gated branch in T011 — no new production code beyond Phase 3).

---

## Phase 5: Version-skew compatibility (Edge cases, FR-004 / SC-005)

- [X] T014 [P] Test: decoding a `host.launch_runner` frame produced by an OLD server (field absent) yields `prefer_binding_token_mint=False` → old-behavior (covered by T005; add an explicit skew-named case if the frame test doesn't already assert the default).
- [ ] T015 Manual/reasoned check: a NEW host receiving a frame from an OLD server never sees the flag set (default False) → inherited path; a NEW server sending to an OLD host has the field ignored on decode. Document the two skew directions in the PR description (no code).

---

## Phase 6: Polish & End-to-End Verification

- [X] T016 Run the full affected unit suites (frame, sessions, connect, `_entry`) — all green: `uv run pytest tests/… -q` for the modules from T002.
- [X] T017 `uv run ruff format` + `uv run ruff check --fix` on the four changed files.
- [ ] T018 Build the omnigent wheel from this branch; deploy server (`omnigent-daveok`) and upload+restart host (`coding-agents`) per quickstart.md "Build + deploy". (Outward-facing — confirm before running.)
- [ ] T019 Run the quickstart headless repro: guest claude-native session on `coding-agents` reaches a running terminal (no `native_terminal_start_failed`), and `[runner:*]` logs show `GET /v1/sessions/{id}/agent/contents → 200` (was 404). Then delete the repro session.
- [ ] T020 Regression: an owner-on-own-host session still succeeds unchanged.
- [ ] T022 [US1] Assert guest ATTRIBUTION, not just success (SC-004): confirm the runner acted as the session owner, not the host SP — e.g. the successful spec callback was authed as the guest (mint path taken, verifiable via the runner log showing the token mint `POST /v1/runners/{id}/token` and/or a server-side audit/attribution line naming the session owner). Distinguishes "works for the right reason" from "works for any reason".

---

## Dependencies & Execution Order

- **Phase 1 → 2**: setup before contract carriers.
- **Phase 2 (T003, T004) blocks everything** in Phases 3–5 (the frame field + env var are referenced by all wiring).
- **Phase 3 (US1)** is the core deliverable. T009/T010/T011 each depend on T003/T004; T011 is independent of T009/T010 (different file) so tests T005–T008 are all [P].
- **Phase 4 (US2)** is satisfied by Phase 3's conditional logic; its tests (T012, T013) just assert the negative path — can be written alongside Phase 3 tests.
- **Phase 6** after all code + unit tests green; T018/T019/T020 are the live E2E gate.

## Parallel Opportunities

- T005, T006, T007, T008 (US1 tests) — different test modules, all [P].
- T012, T013 (US2 tests) — [P] with each other and with the US1 tests.
- Implementation T009 (server), T010 (host), T011 (runner) touch different files — can be done in parallel once T003/T004 land, but each needs its own test green.

## MVP Scope

**US1 alone is the MVP** — it's the entire user-facing value (guest sessions
work). US2 is a regression guard that falls out of US1's conditional design, not
a separable increment. Ship US1 + its regression tests together.

## Format Validation

All tasks use `- [ ] Tnnn [P?] [Story?] description + path`. Setup/Foundational/
Polish tasks are unlabeled; US1/US2 tasks carry their story label.
