---
name: omnigent-databricks-deployment
description: Deploy and troubleshoot the self-managed Omnigent Databricks Apps target in deploy/databricks. Use this instead of generic Databricks sync guidance for wheel, SPA, DAB, Lakebase, UC Volume, OTel, and managed CoDA deployment work.
---

# Omnigent Databricks deployment

## Scope and source of truth

This skill is scoped to the `deploy/databricks/**` deployment target in the
Omnigent repository. Its operational dependency surface also includes the
repository root package, `omnigent/version.py`, `sdks/python-client`,
`sdks/ui`, `web/`, and `tests/deploy/`; changes there can change this target.
It covers the self-managed Databricks Apps deployment, not the managed
Omnigent service and not the other `deploy/<target>/` providers.

Use the files in this directory as the implementation source of truth:

- `README.md` — bootstrap, deploy commands, configuration, rollback, and the
  troubleshooting table.
- `deploy.py` — packaging, versioning, lock generation, bundle invocation,
  permissions, and smoke checks.
- `build.sh` — clean SPA build and the three-wheel build.
- `databricks.yml` — DAB resources, direct engine, targets, app command, and
  resource wiring.
- `src/app.py` — runtime startup, Lakebase credentials, migrations, volume
  path, auth, SPA path, and port behavior.
- `src/app.yaml` — shipped app metadata/reference only. The DAB App resource
  in `databricks.yml` owns the deployed command and environment; in
  particular, `prod-no-otel` intentionally overrides the OTel-on values shown
  in `src/app.yaml`.
- `grant_sp_perms.py` — one-time Lakebase schema grants.
- `tests/deploy/` — deterministic regressions for packaging, versioning,
  OTel, and compute-size behavior.

Read only the relevant sections of `README.md` and the relevant functions in
`deploy.py`; do not re-derive this workflow from generic Databricks App
examples.

## First decision: managed or self-managed

The repository README says most users should use the managed Omnigent on
Databricks offering when it is available. Use this directory only when
self-management is required, such as regional availability, custom policies,
provider keys, or custom egress controls.

Before changing deployment code, state which path is in scope. Do not silently
mix the managed service recipe with this DAB/App recipe.

## Hard invariants

1. **Deploy the staged app, never the repository root.** The DAB resource uses
   `source_code_path: ./src`, where `src` is resolved from this bundle. Do not
   use `databricks sync` or `import-dir` against the checkout for this target.
2. **Deploy only the controlled staging tree.** Never stage `.venv`,
   `node_modules`, `.git`, caches, extracted wheels, or unrelated build output.
   Before any manual staging or upload-like operation, print hidden entries,
   total file count, total size, and files over 10 MB. A prior deployment
   incident grew to about 854 MB and 18,000 files because a hidden `.venv` was
   included. The current `deploy.py` checks wheel and SPA-asset sizes but does
   not reject every arbitrary hidden or oversized file already present in
   `src`; treat any unexpected staged entry as a blocker rather than assuming
   the orchestrator will remove it.
3. **The Workspace limit is per file.** Every wheel and every loose SPA asset
   in `src` must be below the 10 MB Workspace file cap. The total staged tree
   may be larger than 10 MB.
4. **The SPA is outside the main wheel.** A normal `setup.py` build hook puts
   the SPA under `omnigent/server/static/web-ui`, which becomes wheel data.
   `build.sh` must move it to `dist/web-ui` via `WEB_UI_OUT_DIR`; `deploy.py`
   copies it to `src/web-ui`; `src/app.py` sets `OMNIGENT_WEB_UI_DIST` before
   importing the server. Do not manually copy the SPA back into package data.
5. **Clean generated output before rebuilding.** Hashed Vite chunks and stale
   wheels accumulate across builds. Use the normal `deploy.py` build or
   `build.sh`; do not bypass `_clean_build_artifacts` for a release build.
6. **All three local wheels must fit.** The generated app `pyproject.toml`
   uses relative `[tool.uv.sources]` wheel paths and `uv lock` validates them
   locally. A UC Volume wheel path is not a workaround in this flow. If a
   wheel is too large, reduce the Python payload; `--skip-web-ui` only helps
   when the SPA was the cause.
7. **Generated version, wheel metadata, and runtime version agree.** Deploy
   stamps the sibling pyprojects and `omnigent/version.py`. The main wheel's
   `omnigent.version.VERSION` is what `/api/version` reports. Do not reuse
   wheels from a different version or edit generated version files by hand.
8. **`uv.lock` is regenerated for every build.** Wheels are not byte-identical
   because SPA hashes and ZIP timestamps change. A stale lock can preserve old
   hashes and cause an Apps-side `Hash mismatch`. The deploy script removes
   and recreates `src/uv.lock`.
9. **Use the intended workspace explicitly.** Prefer `--profile <profile>`.
   With a profile, ambient `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, and bundle
   engine variables must not override it. Without a profile, supply explicit
   env-based auth (`DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, and
   `DATABRICKS_CLIENT_SECRET`). Verify that the selected profile resolves to
   the same workspace host as the literal `targets.<target>.workspace.host` in
   `databricks.yml`; DAB commands and SDK/permission/smoke-check calls must
   not split across workspaces. Never print tokens.
10. **The bundle engine is `direct`.** The Terraform engine has silently
    dropped `compute_size` on app updates. `databricks.yml` and `deploy.py`
    intentionally assert the direct engine and verify the resulting app size.
11. **Protect multi-app state.** `root_path` is isolated by `app_name`, but the
    local bundle cache is target-scoped. The `prod` and `prod-no-otel` targets
    intentionally share the same App state while using different target names.
    Never deploy with a loaded cache or
    bundle state belonging to another App. On a routine redeploy, abort rather
    than approve any DAB plan showing deletion or replacement of the
    `databricks_app`; confirm the target/app pairing, clear only the stale
    target cache when appropriate, and bind the existing App instead.
12. **One Lakebase project per app.** Do not share an autoscaling project
    between Omnigent Apps. Shared ownership causes migration failures such as
    `permission denied for table agents`.
13. **The first deploy is a bootstrap step.** It creates the App and service
    principal but may fail `/health` with `permission denied for schema public`.
    Run `grant_sp_perms.py` with the correct database spelling, then redeploy.
    The Lakebase resource path uses `databricks-postgres`; the PostgreSQL
    database argument uses `databricks_postgres`.
14. **UC Volume traversal matters.** `WRITE_VOLUME` on the leaf is not enough;
    the App service principal also needs `USE_CATALOG` and `USE_SCHEMA` on its
    parents. `deploy.py` attempts these grants; verify them for a fresh
    catalog or when the deployer lacks `MANAGE`.
15. **OTel is an explicit deployment choice.** A workspace without the
    configured collector/custom OTel storage must use `--no-otel`, which maps
    the default `prod` target to `prod-no-otel`. With a custom target,
    `--no-otel` does nothing unless that target overrides the command, env, and
    telemetry destinations; the script warns about this.
16. **Managed CoDA IDs are immutable fences.** For `--coda-app`, use
    `APP_ID,APP_NAME,APP_URL`; the ID is persisted in host identity. Never
    rename, reuse, repoint, or remove an ID while a host/session can reference
    it. Grant `CAN_USE` in both directions for every pool member. Use `--no-otel`.
    Roll back by draining sessions and leases first, then changing config.
17. **Databricks proxy auth is not generic header auth.** `X-Forwarded-Email`
    is trusted only behind the Databricks Apps proxy. Do not expose the app
    process through a port-forward or alternate ingress that lets callers forge
    that header.

## Canonical workflow

### 1. Preflight

- Confirm the target is self-managed Databricks Apps.
- Confirm Databricks CLI, `uv`, Node 22+, and `pnpm` are available.
- Confirm `databricks.yml` has a literal target workspace host and that it is
  the intended workspace; do not assume a placeholder or stale committed host
  is correct.
- Confirm the intended profile/workspace; do not rely on the default profile.
  Resolve the profile host and compare it with the selected DAB target host;
  stop if the SDK identity and DAB target differ.
- Confirm the bundle state/cache belongs to the requested `--app-name`. Before
  a routine update, inspect the plan and abort on App delete/replace; do not
  accept a name change as an ordinary redeploy.
- Confirm a dedicated Lakebase project and a three-part UC Volume name
  (`catalog.schema.volume`).
- For CoDA, confirm immutable IDs, public URLs, both-direction `CAN_USE`, and
  the required server-client-ID/wheel resources.
- Check the working tree. The deploy normally requires a clean tree at the
  expected revision; use `--allow-dirty` only deliberately and record why.

Before any manual staging or upload, run an equivalent of:

```bash
find deploy/databricks/src -type f -size +10M -print
find deploy/databricks/src -type f | wc -l
du -sh deploy/databricks/src
```

Also inspect hidden entries in the staging directory. A normal staged payload
is a few hundred files and roughly tens of MB, not a checkout containing local
environments or dependency trees.

### 2. One-time infrastructure bootstrap

Create a fresh Lakebase project and wait for its primary endpoint to become
`ACTIVE`. Create the UC schema and volume, for example:

```sql
CREATE SCHEMA IF NOT EXISTS main.omnigent;
CREATE VOLUME IF NOT EXISTS main.omnigent.artifacts;
```

Run the initial deploy once. Expect the first health check to fail until the
App service principal exists and has Lakebase schema privileges. Then run:

```bash
uv run python deploy/databricks/grant_sp_perms.py \
  --app-name omnigent \
  --lakebase-endpoint projects/omnigent/branches/production/endpoints/primary \
  --database databricks_postgres \
  --profile <profile>
```

Redeploy and require `/health` to return 200.

### 3. Build and deploy

Use the repository orchestrator, not a hand-written sequence of `uv build`,
`databricks sync`, and `bundle deploy` commands:

```bash
uv run python deploy/databricks/deploy.py \
  --app-name omnigent \
  --profile <profile> \
  --lakebase-branch projects/omnigent/branches/production \
  --lakebase-database projects/omnigent/branches/production/databases/databricks-postgres \
  --volume-name main.omnigent.artifacts
```

The orchestrator cleans generated output, stamps versions, builds the SPA and
three wheels, classifies wheel sizes, sweeps stale wheels from `src`, copies
the SPA to `src/web-ui`, regenerates `pyproject.toml` and `uv.lock`, binds an
existing App if needed, runs the DAB, verifies `compute_size`, and polls
`/health` plus `/api/version`.

Useful deliberate modes:

```bash
# Reuse already-built wheels only when their version and web payload are known.
uv run python deploy/databricks/deploy.py --skip-build ...

# API-only; no SPA will be served.
uv run python deploy/databricks/deploy.py --skip-web-ui ...

# Workspace without the configured OTel collector/storage.
uv run python deploy/databricks/deploy.py --no-otel ...
```

Do not combine `--skip-build` with an assumption that it will rebuild or repair
`dist/web-ui`; if the existing payload is stale or absent, rebuild normally.

### 4. Post-deploy checks

Require all of the following:

- `bundle deploy` and `bundle run` succeeded.
- The App reports the requested `compute_size`.
- The rendered DAB App command and environment match the selected target; do
  not use `src/app.yaml` to infer the effective `prod-no-otel` command.
- `/health` returns 200.
- `/api/version` equals the stamped deploy version.
- The SPA loads unless API-only mode was intentional.
- The first authenticated artifact-volume operation succeeds.
- No unexpected OTel export errors occur.
- For CoDA, both directions of `CAN_USE`, lease acquisition, and the public
  server callback path work without leaking URLs, identities, or lease tokens.

A direct smoke check can use a Databricks OAuth token, but keep the token out
of shell history, logs, screenshots, and incident reports.

## Troubleshooting map

| Symptom | Likely mistake | Correct response |
|---|---|---|
| Upload hangs or contains thousands of files | Repository root, `.venv`, or `node_modules` was staged | Stop; inspect hidden entries and stage only `deploy/databricks/src` |
| Wheel over 10 MB | SPA/package data or Python payload is inside the wheel | Clean and rebuild; keep SPA loose; reduce Python payload |
| SPA asset over 10 MB | One Vite chunk is too large | Split the chunk in `web/`, or intentionally deploy API-only |
| Old hashed chunks or old wheels remain | Build/source sweep was bypassed | Run the normal clean build; do not manually preserve old artifacts |
| Apps reports `Hash mismatch` | `src/uv.lock` was reused for a newly rebuilt wheel | Remove/recreate the lock through `deploy.py` |
| `/api/version` is the base/dev version | Runtime constant was not stamped or stale wheels were reused | Rebuild without `--skip-build`; verify wheel contents and version |
| `An app with the same name already exists` | Existing App is not bound, or a stale per-target bundle cache belongs to another App | Use the orchestrator bind path; remove only the stale target cache after confirming the target/app, then bind |
| `Resource already managed by Terraform` | App is owned by another bundle directory | Deploy from the owning bundle or explicitly unbind after reviewing ownership |
| Requested LARGE app is MEDIUM | Terraform engine or ambient bundle-engine override | Keep `bundle.engine: direct`; clear ambient engine vars; verify post-deploy size |
| `permission denied for schema public` | App SP has not received one-time Lakebase grants | Run `grant_sp_perms.py`, then redeploy |
| `permission denied for table agents` | Lakebase project/database is shared or tables have the wrong owner | Use a dedicated project; repair ownership only with an explicit recovery plan |
| `Field 'spec.role' cannot be empty` | An unsupported extra Lakebase database was created | Use the project's default database |
| First artifact request is 403 | Parent UC traversal grants are missing | Grant `USE_CATALOG` and `USE_SCHEMA`, not only leaf volume access |
| Package install re-resolves and times out | Apps `exclude-newer` cutoff differs from the generated lock | Read the cutoff from `/logz`, set matching `UV_EXCLUDE_NEWER`, and redeploy |
| Package install tries an unreachable Databricks PyPI proxy | Global uv config wrote proxy URLs into `uv.lock` | Let `deploy.py` sanitize proxy URLs, or set `UV_INDEX_URL` deliberately |
| API-only landing page appears | `src/web-ui` was omitted or stale `--skip-build` was used | Redeploy without `--skip-web-ui` and without `--skip-build` |
| OTel exports fail to localhost:4317 | Workspace has no configured OTel collector/storage | Use `--no-otel`, or configure the target's OTel resources |
| `--no-otel` did not disable OTel | A custom target lacks the `prod-no-otel` overrides | Add command/env/telemetry destination overrides to that target |
| App returns 502 | Startup command/port is wrong | Inspect the rendered `databricks.yml` App config and preserve `DATABRICKS_APP_PORT`/port 8000 behavior; `src/app.yaml` is not authoritative for the DAB command |
| CoDA request falls through unexpectedly | Readiness/auth/malformed failure was treated as capacity | Only a lease-capacity 409 is capacity; fix configuration/auth instead |
| CoDA cleanup targets the wrong App | Mutable name/URL was used instead of persisted immutable ID | Restore the fenced App registry entry; never reroute an existing lease |

## Known implementation gaps

These are deliberate review points, not guarantees supplied by the current
orchestrator:

- `deploy.py` validates wheels and loose SPA assets, but does not yet perform a
  final whole-tree audit for arbitrary hidden files, total source size, file
  count, or unrelated oversized files in `src`. Keep the staging invariant
  above operationally strict, and add a regression before weakening it.
- `--skip-build` validates wheel filenames and the main-wheel runtime version,
  but it does not cryptographically bind `dist/web-ui` to the wheel set. Use it
  only when the SPA payload is known to match; otherwise perform a clean build.
- The CLI checks basic CoDA field presence, while deeper runtime validation may
  reject malformed pool semantics. Validate pool IDs/names/origins before
  changing a production App and treat readiness failure as a configuration
  error, never as capacity.

## Change and review guidance

When modifying this deployment folder:

1. Update the relevant invariant in this skill and `README.md` if behavior
   changes; keep comments short and scenario-focused.
2. Add or update a deterministic test in `tests/deploy/` for regressions that
   can silently deploy the wrong payload, workspace, version, size, engine,
   permission mode, or OTel target.
3. Validate the DAB with the intended target. Do not run a real deploy merely
   to validate YAML when a local test or `databricks bundle validate` is enough.
4. Review generated files as artifacts, not source: `src/pyproject.toml`,
   `src/uv.lock`, wheels, `src/web-ui`, and `dist/` are regenerated by the
   orchestrator and should not be hand-edited or casually committed.
5. Keep deployment evidence sanitized. Never include tokens, credentials,
   lease tokens, raw upstream bodies, private URLs, or customer data in skill
   text, tests, logs, or incident reports.

## Related skills

For this target, load `databricks-core` for authentication/CLI and
`databricks-dabs` for bundle lifecycle. Use generic `databricks-resource-
deployment` only with caution: its `databricks sync` guidance is not the
canonical workflow here. The local Omnigent `build-omnigent` and
`omnigent-knowledge` skills describe agent authoring, not this deployment.
