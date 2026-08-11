# CoDA release milestone verification evidence

Date: 2026-08-11

## Acceptance evidence

- Worktree base: `fc3add8e863802bb5ef39f90ff0f7a574396af9d`
  (`fork/dev` at creation), branch `feature/coda-history-preserving-release`.
- Design contract: `designs/CODA_HISTORY_PRESERVING_RELEASE.md`.
- Operator/rollback runbook: `docs/CODA_RELEASE_RESUME_OPERATIONS.md`.
- Migration upgrade → downgrade to `f7a8b9c0d1e2` → upgrade: pass.
- OpenAPI regeneration, `scripts/dump_openapi.py --check`, and
  `tests/server/test_openapi_drift.py`: pass.

## Deterministic tests

| Command/scope | Result |
|---|---|
| `pytest tests/stores/test_conversation_store.py tests/server/routes/test_coda_release_resume.py tests/onboarding/sandboxes/test_coda.py` | 220 passed |
| `pytest tests/server/test_managed_hosts.py` | 231 passed (pre-existing asyncio-mark warnings) |
| `pytest tests/server/routes/test_sessions_crud.py tests/server/test_openapi_drift.py` | 27 passed |
| Process-separated SQLite detach race | pass; two spawned processes, one retained detached row |
| `vitest run src/components/CodaLifecycleControls.test.tsx` | 5 passed |
| `pytest tests/onboarding/sandboxes/test_coda.py` | 23 passed, including no-spill/manual/full/ambiguous/redaction coverage |

A repository-wide `pytest -q` was attempted. The first attempt identified missing
optional `databricks`/`agents-sdk` extras at collection; those extras were
installed. The fully provisioned attempt exceeded the 30-minute harness limit,
so it is not represented as a pass. The bounded store, lifecycle, managed-host,
CRUD, provider-fencing, migration, and OpenAPI suites above are the authoritative
deterministic regression evidence for this scoped tranche.

## Static/build gates

- `uv run ruff check omnigent tests scripts`: pass.
- Ruff format check on every changed Python file: pass.
- `uv run pyrefly check`: 0 errors (repository-configured suppressions/warnings
  unchanged).
- `pnpm --dir web type-check`: pass.
- `pnpm --dir web lint`: 0 warnings, 0 errors.
- `pnpm --dir web format:check`: pass.
- `pnpm --dir web build`: production build pass (existing CSS highlight/chunk
  size warnings only).

## Fresh-context reviews

Initial independent reports:

- `requirements-review.txt`
- `edge-case-review.txt`
- `security-data-review.txt`
- `regression-review.txt`

The requirements/edge reviews found resume error classification, cleanup
exception masking, and tracker observability issues. They were reproduced and
fixed: manual target preflight returns sanitized 409; post-acquisition failure
returns sanitized 503; cleanup is attempt-host fenced; tracker failure is
settled. Follow-up reports `requirements-rereview.txt` and
`edge-case-rereview.txt` found all prior medium findings fixed and no new
blockers. `final-adjudication.txt` independently accepted issues 9.1–9.6 with no
surviving blockers. Security and regression reviews reported no findings.

## Safety boundaries

No live `daveok` session API or UI was accessed or mutated. No Databricks deploy
was run. No CoDA repository was changed. Only the dedicated
`omnigent-coda-release` worktree was modified. Cross-replica lock hardening,
live E2E, picker scalability, and later operations remain outside this tranche.
