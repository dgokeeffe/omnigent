#!/bin/bash
# provision_coda_hosts.sh
# ---------------------------------------------------------------------------
# Provision extra shared CoDA host apps (coda-03..coda-08) so there is one
# CoDA per workshop team, each self-registering as an Omnigent HOST.
#
# RUN THIS AS AN ADMIN IDENTITY (the daveok/workspace admin), NOT from inside
# a CoDA container. It creates apps, issues IAM grants, and deploys — all of
# which a host SP cannot do.
#
# Per host it:
#   1. Creates the Databricks App (LARGE compute), if absent.
#   2. Grants the app SP the two IAM permissions the host tunnel needs:
#        - CAN_USE on the Omnigent SERVER app  (host tunnel handshake)
#        - READ_VOLUME on the wheel volume      (download the omnigent CLI)
#   3. Deploys coda-01's proven source to the new app (self-registers on boot).
#
# Segregation note: joint access WITHIN a CoDA is intentional here (shared
# terminal, one injected PAT identity). The team boundary is enforced at the
# OMNIGENT layer by the per-user `use` grants in grant_team_hosts.sh — NOT by
# per-CoDA identity. That is the model the facilitator chose.
#
# Idempotent: skips app-create if the app exists; the grant helper skips grants
# already present. Deploy is a snapshot import and is safe to re-run.
# ---------------------------------------------------------------------------
set -euo pipefail

# ── Config (matches coda-01's live app.yaml) ───────────────────────────────
PROFILE="${PROFILE:-daveok}"
SERVER_APP="${SERVER_APP:-omnigent}"
WHEEL_VOLUME="${WHEEL_VOLUME:-edp_aisandbox_aisandbox_dev.daveok.artifacts}"
TEMPLATE_APP="${TEMPLATE_APP:-coda-01}"          # source of the deployed code
COMPUTE_SIZE="${COMPUTE_SIZE:-LARGE}"
# Which hosts to create. coda-01/coda-02 already exist; default fills 03..08
# so 8 teams each get one shared CoDA.
HOSTS=("${@:-coda-03 coda-04 coda-05 coda-06 coda-07 coda-08}")
# shellcheck disable=SC2206
HOSTS=(${HOSTS[@]})

DBX=(databricks --profile "$PROFILE")
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRANT_SH="${HERE}/grant_omnigent_host.sh"   # copied alongside; see note below

echo "==> Profile:        $PROFILE"
echo "==> Server app:     $SERVER_APP"
echo "==> Wheel volume:   $WHEEL_VOLUME"
echo "==> Template app:   $TEMPLATE_APP"
echo "==> Hosts to make:  ${HOSTS[*]}"
echo

# Resolve the template app's deployed source path once (we redeploy the SAME
# reviewed source to every new host so they are byte-identical to coda-01).
TEMPLATE_SRC="$("${DBX[@]}" apps get "$TEMPLATE_APP" --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('active_deployment',{}).get('source_code_path',''))")"
if [[ -z "$TEMPLATE_SRC" ]]; then
  echo "ERROR: could not resolve active source path for template app '$TEMPLATE_APP'." >&2
  echo "       Deploy $TEMPLATE_APP first, or set TEMPLATE_APP to a deployed CoDA." >&2
  exit 1
fi
echo "==> Template source: $TEMPLATE_SRC"
echo

for APP in "${HOSTS[@]}"; do
  echo "──────────────────────────────────────────────────────────────"
  echo "==> Host: $APP"

  # 1. Create the app if it does not already exist.
  if "${DBX[@]}" apps get "$APP" --output json >/dev/null 2>&1; then
    echo "    app exists — skipping create"
  else
    echo "    creating app ($COMPUTE_SIZE)..."
    "${DBX[@]}" apps create "$APP" \
      --description "Shared CoDA host — workshop 13 Jul 2026" \
      >/dev/null
    # Wait for the SP to be provisioned.
    for _ in $(seq 1 30); do
      SP="$("${DBX[@]}" apps get "$APP" --output json 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('service_principal_client_id','') )" || true)"
      [[ -n "$SP" ]] && break
      sleep 4
    done
    echo "    created; SP=$SP"
  fi

  # 2. IAM grants (CAN_USE on server + READ_VOLUME on wheel volume).
  "$GRANT_SH" \
    --profile "$PROFILE" \
    --coda-app "$APP" \
    --server-app "$SERVER_APP" \
    --wheel-volume "$WHEEL_VOLUME"

  # 3. Deploy the reviewed template source (self-registers as a host on boot).
  echo "    deploying source snapshot..."
  "${DBX[@]}" apps deploy "$APP" \
    --source-code-path "$TEMPLATE_SRC" \
    --mode SNAPSHOT \
    --no-wait
  echo "    deploy submitted (--no-wait). Poll: databricks apps get $APP --profile $PROFILE"
done

echo
echo "══════════════════════════════════════════════════════════════"
echo "All hosts submitted. Next:"
echo "  1. Wait for each app to reach RUNNING (compute + deployment SUCCEEDED)."
echo "  2. Paste your PAT into each CoDA terminal (bootstraps run_setup)."
echo "  3. Confirm each appears on the server:  it will show in the Omnigent"
echo "     admin Hosts page (/settings/hosts) and via GET /v1/hosts?all=true."
echo "  4. Run grant_team_hosts.sh to apply the per-team 'use' grants."
