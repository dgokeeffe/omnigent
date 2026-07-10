#!/bin/bash
# grant_team_hosts.sh
# ---------------------------------------------------------------------------
# Apply the per-team Omnigent host share grants that ENFORCE TEAM SEGREGATION.
#
# Each team gets `use` (one member `manage`) on ONLY their own CoDA host, via
# the server's host-permission API:
#     PUT /v1/hosts/{host_id}/permissions/{user_id}   {"level": "..."}
# The API auto-creates the grantee's user row (ensure_user), so guests who have
# never logged in can be granted ahead of time. Grantee ids are the emails the
# Databricks Apps proxy sends as X-Forwarded-Email (the @coles.com.au address).
#
# RUN THIS AS A SERVER ADMIN or a `manage` grantee on each host. It talks to the
# Omnigent SERVER REST API (behind the Databricks Apps OIDC proxy), so it needs
# a *user* bearer token that the proxy accepts, NOT a host-SP token.
#
# Provide the token one of two ways:
#   OMNIGENT_TOKEN=<bearer>            # explicit user token, OR
#   OMNIGENT_PROFILE=<cli-profile>     # a profile whose `databricks auth token`
#                                      # mints a token the server accepts
#
# Reads roster from team_roster.json (host names + user_id/level). Resolves each
# host NAME to its host_id from GET /v1/hosts?all=true (admin) before granting.
#
# Idempotent: PUT upserts a grant (re-running re-sets the same level; no dup).
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROSTER="${ROSTER:-$HERE/team_roster.json}"
SERVER_URL="${SERVER_URL:-$(python3 -c "import json;print(json.load(open('$ROSTER'))['server_url'])")}"

# ── Acquire a user bearer token the server proxy accepts ───────────────────
if [[ -n "${OMNIGENT_TOKEN:-}" ]]; then
  TOKEN="$OMNIGENT_TOKEN"
elif [[ -n "${OMNIGENT_PROFILE:-}" ]]; then
  TOKEN="$(databricks auth token -p "$OMNIGENT_PROFILE" -o json \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")"
else
  echo "ERROR: set OMNIGENT_TOKEN=<bearer> or OMNIGENT_PROFILE=<cli-profile>." >&2
  echo "       The token must be a USER identity the Omnigent server treats as" >&2
  echo "       admin or 'manage' on each host — a host-SP token will 401/redirect." >&2
  exit 1
fi

api() {  # api METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl --http1.1 -sS -m 30 -X "$method" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "$body" "${SERVER_URL}${path}"
  else
    curl --http1.1 -sS -m 30 -X "$method" \
      -H "Authorization: Bearer $TOKEN" "${SERVER_URL}${path}"
  fi
}

# ── Preflight: verify the token is accepted (not an OIDC redirect) ─────────
echo "==> Verifying server access at $SERVER_URL ..."
ME="$(api GET /v1/me || true)"
if [[ "$ME" != \{* ]]; then
  echo "ERROR: /v1/me did not return JSON — token rejected or wrong identity." >&2
  echo "       Got: ${ME:0:200}" >&2
  exit 1
fi
echo "    /v1/me OK"

# ── Resolve host NAME -> host_id from the admin fleet view ─────────────────
echo "==> Resolving host ids (GET /v1/hosts?all=true) ..."
HOSTS_JSON="$(api GET '/v1/hosts?all=true')"
if [[ "$HOSTS_JSON" != \{* ]]; then
  echo "ERROR: could not list hosts (need admin for ?all=true). Got: ${HOSTS_JSON:0:200}" >&2
  exit 1
fi

# ── Walk the roster and grant ──────────────────────────────────────────────
python3 - "$ROSTER" <<'PY' > /tmp/grant_plan.tsv
import json, sys
roster = json.load(open(sys.argv[1]))
for team in roster["teams"]:
    for g in team["grants"]:
        print(f"{team['team']}\t{team['host']}\t{g['user_id']}\t{g['level']}")
PY

FAIL=0
while IFS=$'\t' read -r TEAM HOST_NAME USER LEVEL; do
  HOST_ID="$(HN="$HOST_NAME" python3 -c "
import os,sys,json
d=json.loads(os.environ['HJ'])
hn=os.environ['HN']
m=[h for h in d.get('hosts',[]) if h.get('name')==hn]
print(m[0]['host_id'] if m else '')" HJ="$HOSTS_JSON")"

  if [[ -z "$HOST_ID" ]]; then
    echo "  [SKIP] $TEAM $HOST_NAME — host not registered yet (deploy + PAT-bootstrap it first)"
    FAIL=1
    continue
  fi

  RESP="$(api PUT "/v1/hosts/${HOST_ID}/permissions/${USER}" "{\"level\":\"${LEVEL}\"}" || true)"
  if [[ "$RESP" == *"\"level\""* ]]; then
    echo "  [OK]   $TEAM  $HOST_NAME ($HOST_ID)  $LEVEL  $USER"
  else
    echo "  [ERR]  $TEAM  $HOST_NAME  $USER  ->  ${RESP:0:200}"
    FAIL=1
  fi
done < /tmp/grant_plan.tsv

echo
if [[ "$FAIL" == 0 ]]; then
  echo "==> All grants applied. Verify per host: GET /v1/hosts/{id}/permissions"
else
  echo "==> Completed with some SKIP/ERR rows above (hosts not yet registered, or"
  echo "    insufficient access). Re-run after the missing hosts self-register."
fi
