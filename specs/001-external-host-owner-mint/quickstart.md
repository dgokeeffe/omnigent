# Quickstart: Verify external-host runner mints the session-owner token

End-to-end validation that a guest session on a shared externally-owned host
(CoDA) now starts, using the headless API loop proven this session.

## Prerequisites

- `lakemeter` Databricks CLI profile authenticated (`databricks auth profiles`
  shows `lakemeter` = YES).
- The live apps: `omnigent-daveok` (server) and `coding-agents` (CoDA host),
  both on lakemeter, both ACTIVE.
- Admin on the server (David's `/v1/me` → `is_admin: true`) and a `use` grant
  on the `coding-agents` host (already in place).

## Build + deploy the change

The server and the host both need a build carrying this change.

1. **Server** (`omnigent-daveok`): redeploy from this branch via the repo's
   Databricks deploy path (`deploy/databricks/deploy.py --target lakemeter …`,
   per the deploy skill / memory). The server needs the change so it *sets* the
   frame flag.
2. **Host** (`coding-agents`): build the omnigent wheel from this branch, upload
   it to the UC Volume `OMNIGENTS_WHEEL_SPEC` points at
   (`/Volumes/lakemeter_catalog/daveok_omnigent/artifacts/wheels`), then restart
   `coding-agents` so it reinstalls the runner with the change. The host needs
   the change so it *threads* the flag into the runner env and the runner
   *honors* it.

> Version-skew is safe (FR-004): if only one side is updated, launches degrade
> to today's behavior rather than breaking. But to see the fix, BOTH must carry
> it.

## Run the guest-session repro (headless, over the API)

```
SERVER=https://omnigent-daveok-335310294452632.aws.databricksapps.com
TOKEN=$(databricks auth token -p lakemeter | jq -r .access_token)
AGENT=ag_58a1bc5bf0bba6d31ceeb7661f8d751c            # claude-native-ui
HOST=host_e5955ca57bfaf3665cbae3be6e6856d0           # coding-agents (SP-owned)

# create a claude-native session on the SP-owned host AS David (guest != host owner)
CONV=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$SERVER/v1/sessions" \
  -d "{\"agent_id\":\"$AGENT\",\"host_type\":\"external\",\"host_id\":\"$HOST\",\"workspace\":\"/app/python/source_code\"}" \
  | jq -r .id)

# send a message to trigger the native terminal launch
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$SERVER/v1/sessions/$CONV/events" \
  -d '{"type":"message","data":{"role":"user","content":[{"type":"input_text","text":"print pwd and say hi"}]}}'
```

## Expected outcomes (pass criteria)

- **SC-001 / FR-005**: after ~20–30s, `GET /v1/sessions/$CONV?include_items=true`
  shows `status` progressing to a running terminal — **NOT** `failed` with
  `native_terminal_start_failed`, and **no** `error` item.
- **Runner log confirms 200s**: in `databricks apps logs coding-agents` the
  `[runner:*]` tailer lines show
  `GET /v1/sessions/$CONV/agent/contents "HTTP/1.1 200 OK"` (was 404), and
  there is **no** `harness spawn failed: unknown harness 'runner-test-default'`.
- **SC-003 (regression)**: a session created by the host owner on a host they
  own still succeeds unchanged (owners equal → flag False → inherited path).
- **SC-005 (skew)**: deploying only the server, or only the host, produces no
  new launch failures relative to today.

## Unit-test checkpoints (run before deploy)

- `_launch_runner_on_host` sets `prefer_binding_token_mint` True iff
  `session_owner != host_owner`.
- `HostLaunchRunnerFrame` encode/decode round-trips the new field and decodes an
  older frame (missing field) as `False`.
- `_build_runner_env` sets the env var only when the flag is True.
- `_make_auth_token_factory` returns the mint factory when the flag env is set +
  binding token present, even when an inherited credential would resolve;
  unchanged when the flag is unset.

## Cleanup

Delete the repro session(s): `curl -X DELETE -H "Authorization: Bearer $TOKEN"
"$SERVER/v1/sessions/$CONV"`.
