---
name: databricks-app-deploy
description: Deploy or redeploy the Omnigent server to Databricks Apps (deploy/databricks/) from a machine behind the Databricks PyPI proxy. Captures the two environment facts that otherwise get rediscovered every time — pypi.org is blocked locally so the proxy must be used for the wheel build + lock, and the proxy's /packages URLs 404 inside the Apps container so the generated uv.lock must be normalized to pypi.org before bundle deploy. Load when the user asks to deploy/redeploy/ship Omnigent to a Databricks workspace (aws-daveok, lakemeter, prod, or a new sandbox), when a Databricks Apps deploy fails with "Error installing packages" / a proxy 404, or when adding a new deploy target. NOT for non-Omnigent Databricks tables or generic bundle work.
---

# Deploy Omnigent to Databricks Apps (behind the PyPI proxy)

`deploy/databricks/deploy.py` builds three wheels, generates
`src/pyproject.toml` + `src/uv.lock`, then runs `databricks bundle
deploy` + `bundle run` against a target in `databricks.yml`. The full
reference is `deploy/databricks/README.md` — read it for the resource
model, token lifecycle, and troubleshooting table. This skill only adds
the two environment-specific facts that make a deploy from *this machine*
work, plus the exact working command sequence.

## The two facts that waste tokens if rediscovered

### 1. pypi.org is blocked locally — build/lock through the proxy

`/etc/hosts` maps `pypi.org` (and many mirrors) to `127.0.0.1`, so any
`uv build` / `uv lock` that defaults to public PyPI fails with
`Connection refused (os error 61)`. The sanctioned index is the
Databricks proxy, set as the default index in `~/.config/uv/uv.toml`:

```
https://pypi-proxy.cloud.databricks.com/simple/
```

`build.sh` and `deploy.py`'s `run_uv_lock` do **not** inherit that
global default (a project-level `uv.toml` shadows it, and `run_uv_lock`
explicitly drops `UV_DEFAULT_INDEX`/`UV_INDEX`). So you must pass the
proxy explicitly. `run_uv_lock` reads `UV_INDEX_URL`:

```bash
export UV_INDEX_URL="https://pypi-proxy.cloud.databricks.com/simple/"
export UV_DEFAULT_INDEX="$UV_INDEX_URL"   # for build.sh's uv build steps
```

Do **not** try to unblock pypi.org by editing `/etc/hosts` — the block
is intentional in this sandbox. The Fastly IP works and
`files.pythonhosted.org` is reachable, which matters for fact #2.

### 2. The proxy's package URLs 404 in-container — normalize the lock

`deploy.py` normally locks against `pypi.org` so the URLs baked into
`uv.lock` resolve inside the Apps container. If you lock against the
proxy (which you must, per fact #1), every wheel URL becomes
`https://pypi-proxy.cloud.databricks.com/packages/...`, and the Apps
container returns **404** for those paths — the deploy fails at
`bundle run` with `Error installing packages` (build logs show
`Failed to download <pkg>` / `404 Not Found` for a proxy `/packages/`
URL).

Fix: after the proxy lock is generated, rewrite the registry + wheel
URLs to the canonical public hosts (`pypi.org/simple` +
`files.pythonhosted.org`). The path component is identical between the
proxy and the canonical host, so the rewrite is exact. The repo ships a
normalizer for this:

```bash
uv run python scripts/normalize_uv_lock_registry.py deploy/databricks/src/uv.lock
# verify clean:
uv run python scripts/normalize_uv_lock_registry.py --check deploy/databricks/src/uv.lock
```

The catch: `deploy.py` re-runs `uv lock` on every invocation, so you
**cannot** normalize and then call `deploy.py` again (it re-poisons the
URLs). Instead, let `deploy.py` do the build + lock, then normalize and
drive `bundle deploy` / `bundle run` **directly**, bypassing the re-lock.

## Working command sequence (redeploy)

Substitute the target's own values. `aws-daveok` is the worked example.

Facts for the aws-daveok deploy (a known-good reference):
- profile / target: `aws-daveok`
- host: `https://fe-sandbox-dok-aws-sandbox.cloud.databricks.com`
- lakebase branch: `projects/omnigent/branches/production`
- lakebase database (resource path, **hyphen**): `projects/omnigent/branches/production/databases/databricks-postgres`
- volume: `dok_aws_sandbox_catalog.omnigent.artifacts`
- otel schema: `dok_aws_sandbox_catalog.omnigent`
- app URL: `https://omnigent-7474660536734442.aws.databricksapps.com`
- app SP: `4d8b9358-c114-4cba-ba1a-2b24d35603a3`

```bash
cd /Users/david.okeeffe/Repos/omnigent
export UV_INDEX_URL="https://pypi-proxy.cloud.databricks.com/simple/"
export UV_DEFAULT_INDEX="$UV_INDEX_URL"

# 1) Build wheels + generate lock via deploy.py, but stop before bundle deploy
#    re-poisons nothing yet — the lock is proxy-flavored here. Run the full
#    deploy.py; it WILL fail at `bundle run` with the 404. That's fine — the
#    build + copied wheels + src/pyproject.toml are what we keep. (Or run
#    build steps then let it fail fast — the failing run leaves src/ populated.)
uv run python deploy/databricks/deploy.py \
    --app-name omnigent --profile aws-daveok --target aws-daveok \
    --lakebase-branch projects/omnigent/branches/production \
    --lakebase-database projects/omnigent/branches/production/databases/databricks-postgres \
    --volume-name dok_aws_sandbox_catalog.omnigent.artifacts \
    --otel-table-schema dok_aws_sandbox_catalog.omnigent \
    --allow-dirty --no-smoke-check || true

# 2) Normalize the generated lock to public PyPI URLs (fixes the 404).
uv run python scripts/normalize_uv_lock_registry.py deploy/databricks/src/uv.lock
uv run python scripts/normalize_uv_lock_registry.py --check deploy/databricks/src/uv.lock

# 3) Drive bundle deploy + run DIRECTLY (no deploy.py — it would re-lock).
VARS=(--var app_name=omnigent \
      --var lakebase_branch=projects/omnigent/branches/production \
      --var lakebase_database=projects/omnigent/branches/production/databases/databricks-postgres \
      --var volume_name=dok_aws_sandbox_catalog.omnigent.artifacts \
      --var otel_table_schema=dok_aws_sandbox_catalog.omnigent)

databricks bundle deploy --target aws-daveok --profile aws-daveok "${VARS[@]}"
databricks bundle run   omnigent --target aws-daveok --profile aws-daveok "${VARS[@]}"
```

Run `bundle deploy`/`bundle run` from `deploy/databricks/` (the bundle
root). The `--var` set must match `deploy.py`'s `_bundle_vars`.

## Verify (don't declare done on `bundle run` exit 0)

`bundle run` returning "App started successfully" does not mean the app
is healthy — it can boot and then crash on the migrate hook. Always hit
`/health`:

```bash
TOKEN="$(databricks auth token aws-daveok --output json \
  | uv run python -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')"
curl --http1.1 -fsS -H "Authorization: Bearer $TOKEN" \
  https://omnigent-7474660536734442.aws.databricksapps.com/health
# expect: {"status":"ok"}  (HTTP 200)
```

A **502** with app state RUNNING = the process is crash-looping; pull
build logs (Databricks UI → app → Logs, or the deployment record) to see
why. On a *first* deploy to a new workspace, 502 is expected until the SP
grant (below) is done.

## First-time deploy to a NEW workspace (bootstrap)

A "redeploy" to a workspace that has never had Omnigent is really a
first-time deploy. Check first — don't assume:

```bash
databricks apps list --profile <p>                       # app exists?
databricks postgres list-projects --profile <p>          # lakebase project?
databricks volumes read <cat>.<schema>.artifacts --profile <p>   # volume?
```

If missing, bootstrap in order (see README §"One-time bootstrap"):

1. **Add a target** to `deploy/databricks/databricks.yml` — literal
   `workspace.host` (DAB reads it before resolving vars), per-app
   `root_path`. Add the `telemetry_export_destinations` block only if the
   catalog has a **custom storage location** (default-storage catalogs
   reject app telemetry export); point the otel tables at
   `<catalog>.<schema>` via `--otel-table-schema`.
2. **Lakebase project** (one per app, never shared). SDK signatures drift
   from the README — `wc.postgres.create_project(...)` then poll
   `get_endpoint(name="projects/<app>/branches/production/endpoints/primary")`
   for `ACTIVE`. The polling loop can outlast a 120s tool timeout; the
   project itself is created near-instantly, so re-check state in a
   fresh short command rather than assuming failure.
3. **UC volume** — `<catalog>.<schema>.artifacts` (may already exist).
4. **First deploy** (steps 1-3 of the sequence above) — app + SP get
   created; `/health` 502s because the SP has no Lakebase grants yet.
5. **Grant the SP** — note the **underscore** DB name here vs the
   hyphenated resource path above:
   ```bash
   uv run python deploy/databricks/grant_sp_perms.py \
       --app-name omnigent \
       --lakebase-endpoint projects/omnigent/branches/production/endpoints/primary \
       --database databricks_postgres \
       --profile aws-daveok
   ```
6. **Restart** — `databricks bundle run omnigent ...` again; the migrate
   hook now succeeds and `/health` returns 200.

## Publishing wheels to the volume (for external CoDA consumers)

The app installs its own wheels from the **workspace source snapshot**
(never a volume — `uv lock` rejects volume path sources at build time).
If a *separate* runtime needs to `uv pip install` Omnigent from the
volume (e.g. a coding-agents-on-Databricks-Apps process), pass
`--publish-wheels-to-volume` to `deploy.py`. It uploads all three built
wheels to:

```
/Volumes/<catalog>/<schema>/<volume>/wheels/<deploy_version>/
```

Version-namespaced so deploys coexist. This is additive — it does not
change how the app itself installs. Consume at runtime with:

```bash
uv pip install \
  /Volumes/dok_aws_sandbox_catalog/omnigent/artifacts/wheels/<version>/omnigent-<version>-py3-none-any.whl
```

## Gotchas recap

- Two DB spellings: **hyphen** `databricks-postgres` for the DAB resource
  path (`--lakebase-database`, `databricks.yml`); **underscore**
  `databricks_postgres` for `grant_sp_perms.py --database` and the app's
  `PGDATABASE`.
- `deploy.py` has `--otel-table-schema` (a real flag), not a passthrough
  `--var otel_table_schema=…`. Don't pass the `--var` form to `deploy.py`
  (it errors "unrecognized arguments"); the `--var` form is only for the
  direct `bundle deploy`/`run` calls.
- Health-check smoke flag is `--no-smoke-check`, not `--skip-health-check`.
- The working tree is often dirty / not on `origin/main`; `deploy.py`
  enforces a clean-tree gate — pass `--allow-dirty` to deploy the current
  fork state.
- Don't commit `deploy/databricks/src/{pyproject.toml,uv.lock,*.whl}` —
  they're per-deploy artifacts. The `databricks.yml` target addition IS
  worth committing.
