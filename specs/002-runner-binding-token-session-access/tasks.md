---
description: "Task list for Runner binding-token session access (header-mode)"
---

# Tasks: Runner binding-token session access (header-mode)

**Input**: Design docs from `specs/002-runner-binding-token-session-access/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED (security-critical auth change — TDD-leaning).

**Organization**: Server access-grant + runner header, threaded along the
callback path. US1 (guest works), US2 (tightly-scoped security), US3 (no
regression). Builds on branch `001-external-host-owner-mint` (reuses the
`prefer_binding_token_mint` flag + `RUNNER_TUNNEL_TOKEN_HEADER`).

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [ ] T001 Confirm edit sites at HEAD: `_require_access_and_level_sync` + `require_access_and_level` in `omnigent/server/routes/_auth_helpers.py`; the 3 spec-callback call sites in `omnigent/server/routes/sessions.py` (get_session, get_session_agent, get_session_agent_contents); the `server_client` construction in `omnigent/runner/_entry.py`.
- [ ] T002 Locate test modules to extend: `_auth_helpers` access tests, sessions spec-callback tests, `tests/runner/test_runner_entry.py`.

## Phase 2: Foundational

- [ ] T003 In `omnigent/server/routes/_auth_helpers.py`, import `LEVEL_READ` (from `omnigent.server.auth`) and `token_bound_runner_id` (from `omnigent.runner.identity`).

## Phase 3: US1 — Guest session works in header mode (P1)

### Tests (write first)

- [ ] T004 [P] [US1] Test: `_require_access_and_level_sync` with a `runner_binding_token` whose `token_bound_runner_id` == `conv.runner_id` and `required_level=LEVEL_READ` returns `SessionAccess(level=LEVEL_READ, conversation=conv)` EVEN WHEN `user_id=None` (header-mode, no identity). In the `_auth_helpers` test module.
- [ ] T005 [P] [US1] Test: the 3 spec-callback routes pass the `X-Omnigent-Runner-Tunnel-Token` header into the access helper (integration or via a spy) so a matching runner token yields 200. In the sessions test module.
- [ ] T006 [P] [US1] Test: the runner `server_client` includes `X-Omnigent-Runner-Tunnel-Token: <binding_token>` when `OMNIGENT_RUNNER_PREFER_BINDING_TOKEN_MINT` is set + a binding token resolves; omits it when unset. In `test_runner_entry`.

### Implementation

- [ ] T007 [US1] In `_require_access_and_level_sync`, add optional `runner_binding_token: str | None = None`. After the `permission_store is None` short-circuit and BEFORE the `user_id is None` 401: if `runner_binding_token` and `required_level <= LEVEL_READ`, fetch `conv`; if `conv and conv.runner_id and token_bound_runner_id(runner_binding_token.strip()) == conv.runner_id`, return `SessionAccess(level=LEVEL_READ, conversation=conv)`. Else fall through unchanged.
- [ ] T008 [US1] In `require_access_and_level` (async wrapper), add the param and pass it through `asyncio.to_thread`.
- [ ] T009 [US1] In `sessions.py`, at the 3 spec-callback call sites, pass `runner_binding_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)`.
- [ ] T010 [US1] In `omnigent/runner/_entry.py`, add `RUNNER_TUNNEL_TOKEN_HEADER: <binding_token>` to the `server_client` `headers=` when `os.environ.get(RUNNER_PREFER_BINDING_TOKEN_MINT_ENV_VAR)` and `_runner_tunnel_binding_token_from_env()` resolves.

## Phase 4: US2 — Tightly-scoped security (P1)

- [ ] T011 [P] [US2] Test: a token bound to session X (matches X's runner_id) does NOT grant access to session Y (Y has a different runner_id) — falls through to 404. In the `_auth_helpers` test module.
- [ ] T012 [P] [US2] Test: a matching token with `required_level > LEVEL_READ` (e.g. LEVEL_OWNER) does NOT grant — falls through to the normal deny. In the `_auth_helpers` test module.
- [ ] T013 [P] [US2] Test: absent / empty / malformed `runner_binding_token` leaves resolution identical to today (fail closed). In the `_auth_helpers` test module.

## Phase 5: US3 — No regression (P1)

- [ ] T014 [P] [US3] Test: with `runner_binding_token=None`, owner / non-owner / admin / unauthenticated all resolve to the same level/exception as before the change. In the `_auth_helpers` test module.

## Phase 6: Polish & End-to-End Verification

- [ ] T015 Run the affected unit suites (`_auth_helpers`, sessions, `test_runner_entry`) — all green.
- [ ] T016 `uv run ruff format` + `ruff check --fix` on the changed files.
- [ ] T017 Rebuild the 3 wheels (`SKIP_WEB_UI=1 build.sh`); delete old + upload new to the CoDA UC Volume; redeploy `coding-agents` (force-reinstall) and `omnigent-daveok` (deploy.py --target lakemeter). (Outward-facing — confirm before running.)
- [ ] T018 E2E (quickstart): guest claude-native session on `coding-agents` reaches a running terminal; `[runner:*]` logs show `GET /v1/sessions/{id}/agent/contents → 200` (was 404). (SC-001)
- [ ] T019 Regression: an owner-on-own-host / normal user session still works unchanged (SC-004). Then delete the E2E session.

## Dependencies

- T003 blocks T007. T007 blocks T008/T009 (they call/pass into it). T010 (runner) independent of the server tasks.
- Tests T004-T006, T011-T014 are [P] (different assertions; write alongside impl).
- Phase 6 after code + unit tests green; T017-T019 are the live gate.

## MVP

US1 + US2 together (guest works + the security guardrails that make it safe).
US3 is a regression guard. Ship all three.
