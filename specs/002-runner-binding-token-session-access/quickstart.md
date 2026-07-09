# Quickstart: Verify header-mode runner binding-token session access

Proves a guest native session on the SP-owned `coding-agents` host reaches a
running terminal on the **header-mode** `omnigent-daveok` server.

## Prerequisites

- `lakemeter` CLI profile authenticated; `omnigent-daveok` (server) +
  `coding-agents` (host) ACTIVE on lakemeter.
- David is admin + holds `use` on the `coding-agents` host (guest ≠ host owner,
  SP-owned host).
- The CoDA runner-log tailer + `OMNIGENTS_FORCE_REINSTALL=1` are already
  deployed (from the earlier CoDA work), so a new runner wheel actually
  installs and its logs are visible.

## Build + deploy

Both server and host need this build.

1. Build the 3 wheels from this branch: `SKIP_WEB_UI=1 bash deploy/databricks/build.sh`.
2. **Host**: delete the old wheels in the UC Volume
   `/Volumes/lakemeter_catalog/daveok_omnigent/artifacts/wheels` and upload the
   new ones (delete first — `sorted()[-1]` picks lexically-last, so mixed
   versions can select the wrong wheel). Redeploy `coding-agents` (force-reinstall
   installs the new runner).
3. **Server**: redeploy `omnigent-daveok` via `deploy/databricks/deploy.py
   --target lakemeter …` (the trailing UC-grant step 403 is the known cosmetic
   failure — the app still redeploys/restarts). The server needs the change so
   the read-path helper honors the binding-token header.

> Run app-directed `curl`/verification UNSANDBOXED — the Databricks Apps host
> isn't in the sandbox allowlist, so in-sandbox calls flake.

## Run the guest-session E2E (headless)

```
SERVER=https://omnigent-daveok-335310294452632.aws.databricksapps.com
TOKEN=$(databricks auth token -p lakemeter | jq -r .access_token)
AGENT=ag_58a1bc5bf0bba6d31ceeb7661f8d751c   # claude-native-ui
HOST=host_e5955ca57bfaf3665cbae3be6e6856d0  # coding-agents (SP-owned)

CONV=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$SERVER/v1/sessions" \
  -d "{\"agent_id\":\"$AGENT\",\"host_type\":\"external\",\"host_id\":\"$HOST\",\"workspace\":\"/app/python/source_code\"}" | jq -r .id)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$SERVER/v1/sessions/$CONV/events" \
  -d '{"type":"message","data":{"role":"user","content":[{"type":"input_text","text":"print pwd and say hi"}]}}'
```

## Pass criteria

- **SC-001**: after ~20-30s the session is NOT `failed` with
  `native_terminal_start_failed`; it reaches a running terminal. In
  `databricks apps logs coding-agents` the `[runner:*]` tailer shows
  `GET /v1/sessions/$CONV/agent/contents → 200` (was 404), no
  `runner-test-default` fallback.
- **SC-002 (security)**: unit test — a binding token bound to session X, checked
  against session Y (different `conv.runner_id`), does not grant access.
- **SC-003 (security)**: unit test — the binding-token grant does not satisfy a
  `> LEVEL_READ` requirement.
- **SC-004 (regression)**: no-token requests (owner / non-owner / admin /
  unauthorized) resolve exactly as before.
- **SC-005**: after a relaunch (new `conv.runner_id`), the superseded token no
  longer matches; the new one does.

## Unit tests (run before deploy)

- Match → read grant; wrong-session token → no grant; `> LEVEL_READ` → no grant;
  absent/empty/malformed token → unchanged; no-token path unchanged.
- Runner attaches `X-Omnigent-Runner-Tunnel-Token` only when the prefer flag is
  set.

## Cleanup

`curl -X DELETE -H "Authorization: Bearer $TOKEN" "$SERVER/v1/sessions/$CONV"`.
