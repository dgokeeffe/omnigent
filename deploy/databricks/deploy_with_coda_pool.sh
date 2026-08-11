#!/usr/bin/env bash
# Deploy this Omnigent app with a managed CoDA pool, discovering the pool from
# the workspace instead of hand-writing one --coda-app flag per sandbox.
#
# Everything workspace-specific is resolved at run time — the workspace host,
# each CoDA App's URL, and this app's public URL all come from the Databricks
# API or from flags. Nothing here may hardcode a host, workspace id, or App URL:
# a committed workspace value is trusted with no probe and silently deploys
# somewhere unintended (see the deploy skill's "use the intended workspace
# explicitly" invariant), and tests/deploy asserts this file stays literal-free.
#
# Pool identity: each binding's immutable id is the Databricks App NAME. That id
# is persisted in managed host identity as `coda:<app_id>#<lease>`, so renaming a
# pooled App breaks the fence for every host that references it. Rename nothing;
# add or remove members instead (see README.md "For rollback").
#
# Examples:
#   # every App named coda-* becomes a pool member, in name order
#   ./deploy/databricks/deploy_with_coda_pool.sh \
#       --coda-prefix coda- \
#       --lakebase-branch projects/omnigent/branches/production \
#       --lakebase-database projects/omnigent/branches/production/databases/databricks-postgres \
#       --volume-name main.omnigent.artifacts
#
#   # an explicit pool, extra-large app, reusing already-built wheels
#   ./deploy/databricks/deploy_with_coda_pool.sh \
#       --coda-app coda-01 --coda-app coda-02 \
#       --compute-size XLARGE \
#       --lakebase-branch ... --lakebase-database ... --volume-name ... \
#       -- --skip-build
#
# Anything after `--` is passed through to deploy.py unchanged.
set -euo pipefail

APP_NAME="omnigent"
CODA_PREFIX=""
CODA_APP_NAMES=()
LAKEBASE_BRANCH=""
LAKEBASE_DATABASE=""
VOLUME_NAME=""
COMPUTE_SIZE="LARGE"
PUBLIC_URL=""
PROFILE=""
PATCH_HOST=1
PRINT_ONLY=0

usage() {
  sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-name)          APP_NAME="$2"; shift 2 ;;
    --coda-prefix)       CODA_PREFIX="$2"; shift 2 ;;
    --coda-app)          CODA_APP_NAMES+=("$2"); shift 2 ;;
    --lakebase-branch)   LAKEBASE_BRANCH="$2"; shift 2 ;;
    --lakebase-database) LAKEBASE_DATABASE="$2"; shift 2 ;;
    --volume-name)       VOLUME_NAME="$2"; shift 2 ;;
    --compute-size)      COMPUTE_SIZE="$2"; shift 2 ;;
    --public-url)        PUBLIC_URL="$2"; shift 2 ;;
    --profile)           PROFILE="$2"; shift 2 ;;
    --no-host-patch)     PATCH_HOST=0; shift ;;
    --print-only)        PRINT_ONLY=1; shift ;;
    -h|--help)           usage 0 ;;
    --)                  shift; break ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

for required in LAKEBASE_BRANCH LAKEBASE_DATABASE VOLUME_NAME; do
  if [[ -z "${!required}" ]]; then
    echo "ERROR: --$(echo "$required" | tr 'A-Z_' 'a-z-') is required" >&2
    usage 1
  fi
done
if [[ -z "$CODA_PREFIX" && ${#CODA_APP_NAMES[@]} -eq 0 ]]; then
  echo "ERROR: pass --coda-prefix or at least one --coda-app" >&2
  usage 1
fi

cd "$(dirname "$0")/../.."
DBX=(databricks)
[[ -n "$PROFILE" ]] && DBX+=(--profile "$PROFILE")

# The workspace host: an explicit DATABRICKS_HOST wins, otherwise ask the CLI
# which workspace the resolved credentials actually point at. Never a literal.
WORKSPACE_HOST="${DATABRICKS_HOST:-}"
if [[ -z "$WORKSPACE_HOST" ]]; then
  WORKSPACE_HOST="$("${DBX[@]}" auth describe --output json \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("details") or {}).get("host") or d.get("host") or "")')"
fi
if [[ -z "$WORKSPACE_HOST" ]]; then
  echo "ERROR: could not resolve the workspace host; set DATABRICKS_HOST or pass --profile" >&2
  exit 1
fi
echo "==> workspace: $WORKSPACE_HOST"

# databricks.yml ships a placeholder host and DAB reads it literally, before
# variables resolve — so it cannot be a ${var.}. Patch the checkout at deploy
# time rather than committing one workspace's URL. Idempotent, and it survives a
# `git reset` of the branch (which reverted it once mid-deploy and sent a deploy
# at the placeholder host).
if [[ "$PATCH_HOST" == "1" ]]; then
  python3 - "$WORKSPACE_HOST" <<'PY'
import pathlib, re, sys
host = sys.argv[1]
path = pathlib.Path("deploy/databricks/databricks.yml")
text = path.read_text()
patched, count = re.subn(r"(?m)^(  host: )\S+", r"\1" + host, text)
if count != 1:
    raise SystemExit(f"expected exactly one workspace host line in {path}, found {count}")
if patched != text:
    path.write_text(patched)
    print(f"==> set the DAB target host to {host}")
PY
fi

# Pool discovery: the App's own `url` from the API is authoritative. Building it
# from a name plus a workspace id would bake in cloud/region spelling and break
# on any workspace whose App URLs differ.
APPS_JSON="$("${DBX[@]}" apps list --output json)"
mapfile -t POOL_ARGS < <(
  APPS_JSON="$APPS_JSON" \
  SERVER_APP="$APP_NAME" \
  CODA_PREFIX="$CODA_PREFIX" \
  CODA_APPS="$(printf '%s\n' "${CODA_APP_NAMES[@]+"${CODA_APP_NAMES[@]}"}")" \
  python3 - <<'PY'
import json, os, sys

wanted = [name for name in os.environ.get("CODA_APPS", "").splitlines() if name]
prefix = os.environ.get("CODA_PREFIX", "")
server_app = os.environ["SERVER_APP"]

apps = {}
for app in json.loads(os.environ["APPS_JSON"]):
    name, url = app.get("name"), app.get("url")
    if name and url:
        apps[name] = url.rstrip("/")

selected = wanted or sorted(n for n in apps if prefix and n.startswith(prefix))
missing = [n for n in selected if n not in apps]
if missing:
    sys.exit("CoDA Apps not found on this workspace: " + ", ".join(missing))
selected = [n for n in selected if n != server_app]
if not selected:
    sys.exit("no CoDA Apps matched; check --coda-prefix / --coda-app")
for name in selected:
    # app_id == app name: an immutable fence persisted in host identity.
    print("--coda-app")
    print(name + "," + name + "," + apps[name])
PY
)
# Process substitution hides the exit status, so an empty pool is the signal that
# discovery failed. Deploying on past it would wipe the App's configured pool.
if [[ ${#POOL_ARGS[@]} -eq 0 ]]; then
  echo "ERROR: CoDA pool discovery produced no members; refusing to deploy" >&2
  exit 1
fi
echo "==> pool members: $(( ${#POOL_ARGS[@]} / 2 ))"

# The public URL managed CoDA hosts dial back on. Read it from the App unless
# overridden; on a first-ever deploy the App does not exist yet, so pass
# --public-url explicitly in that case.
if [[ -z "$PUBLIC_URL" ]]; then
  PUBLIC_URL="$("${DBX[@]}" apps get "$APP_NAME" --output json 2>/dev/null \
    | python3 -c 'import json,sys; print((json.load(sys.stdin).get("url") or "").rstrip("/"))' 2>/dev/null || true)"
fi
if [[ -z "$PUBLIC_URL" ]]; then
  echo "ERROR: could not resolve the public URL for app '$APP_NAME'; pass --public-url" >&2
  exit 1
fi

# --no-otel is not optional here: managed CoDA deploys use the tracer-off target
# (see README.md and the deploy skill). Add --otel-table-schema and drop this
# only on a workspace that really has the collector and UC OTel tables.
DEPLOY_ARGS=(
  --app-name "$APP_NAME"
  --no-otel
  --allow-dirty
  --compute-size "$COMPUTE_SIZE"
  --lakebase-branch "$LAKEBASE_BRANCH"
  --lakebase-database "$LAKEBASE_DATABASE"
  --volume-name "$VOLUME_NAME"
  "${POOL_ARGS[@]}"
  --omnigent-public-server-url "$PUBLIC_URL"
)
[[ -n "$PROFILE" ]] && DEPLOY_ARGS+=(--profile "$PROFILE")

if [[ "$PRINT_ONLY" == "1" ]]; then
  printf '%s\n' "deploy.py ${DEPLOY_ARGS[*]} $*"
  exit 0
fi

exec uv run --extra databricks python deploy/databricks/deploy.py "${DEPLOY_ARGS[@]}" "$@"
