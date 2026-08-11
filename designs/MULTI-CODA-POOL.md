# Multi-CoDA Pool Architecture (omnigent-0hd.1)

## Feasibility verdict and scope

**GO for the REQUIRED MVP below; NO-GO for the previously proposed durable scheduler/health/alias/UI/API expansion.** Existing Omnigent and CoDA capabilities support a small pool safely. Durable fairness, durable health, migration tables, UI and API additions are not approved MVP requirements.

Invariant: sessions sharing one CoDA App may share that App's underlying storage. Different Apps are isolated. Once a lease is acquired, every connect, workspace, adoption, relaunch, disconnect and cleanup operation stays fenced to the immutable App identity that granted it; fallback applies only to a NEW acquisition.

## Existing capability evidence

| Proposed mechanism | Existing implementation and conclusion |
|---|---|
| Persist the granting App | `hosts.sandbox_id` already persists an opaque 256-character provider ID (`db_models.py`, `host_store.py`, existing migration `k1a2...`). Use `coda:<app_id>#<lease_id>` there; **no schema/migration table required**. |
| App fence | `coda.py::_parse_sandbox_id`, `start_host`, `allocate_workspace`, and `terminate` already parse/check `coda:<app-name>#<lease-id>`. Replace name lookup with configured immutable `app_id`; lifecycle callers already pass persisted `sandbox_id`. |
| Capacity authority | CoDA `POST /api/omnigent-host/lease` returns 409 when its one active lease cannot be acquired; this is authoritative capacity. `/status` supplies readiness. No durable health service is needed. |
| Idempotent acquire/reconciliation | CoDA has **no lease-status/list-by-idempotency-key endpoint and no separate idempotency key**. `acquire_lease(owner, lease_id)` is owner-CAS: retrying the same POST returns the active lease for that owner. Omnigent already creates a client `lease_id` and rejects a returned different generation. Therefore MVP retries an ambiguous request once on the **same App with the same lease_id**: matching ID confirms success; different ID or continued ambiguity fails without trying another App. A dedicated query would be a CoDA-repository dependency, not hidden Omnigent work. |
| Concurrency | Session adoption already uses `request.app.state.coda_adoption_lock` and `coda_owner_launches`. The pool adds one process-local `threading.Lock` around cursor selection/advance. No scheduler rows are required. |
| Lifecycle persistence | `register_managed_host` persists provider/sandbox ID before start; launch/relaunch and teardown use that ID. Existing routes cover adoption (`routes_core.py`), session deletion/shared-host retention (`routes_events.py`), and terminate/orphan cleanup (`managed_hosts.py`). |
| App readiness | Existing `CodaProvider.prepare()` checks Databricks App `ACTIVE` and `/api/omnigent-host/status.ready`. MVP probes candidates at NEW acquisition time with existing 30-second request bounds; no persisted health. |

## REQUIRED MVP contract

### Configuration and identity

`sandbox.coda.pool` is a non-empty list of `{app_id, app_name, app_url}`. `app_id` is operator-configured, immutable, opaque, unique, and never reused; `app_name` is the current Databricks lookup/display name and may change; `app_url` is the endpoint. Reject partial entries, duplicate IDs, duplicate names, duplicate normalized URLs, invalid URLs, unknown keys, and simultaneous legacy singleton plus pool. The registry is immutable after config parsing.

Legacy singleton `{app_name, app_url}` normalizes in memory to one entry with `app_id = app_name`. Existing persisted `coda:<app-name>#<lease-id>` therefore remains valid when that same legacy identity is retained. Before renaming/removing a legacy App, the operator must configure its new pool entry with `app_id` equal to the old name until all old leases are gone. This is an operational rolling migration—**no data migration and no alias table**. A removed `app_id` makes old lifecycle operations fail closed with a clear diagnostic; it never reroutes them.

### Selection, failures and restart

A provider instance holds a process-local round-robin cursor protected by a lock. Under the lock it snapshots the starting index and advances once per NEW request; candidates are attempted in circular configured order, so configured order plus `app_id` is deterministic tie-breaking. Concurrent requests receive distinct starts. Each candidate is attempted at most once; total attempts are bounded by pool size.

At NEW acquisition only:

1. Probe App ACTIVE/readiness. An unavailable, timed-out, or stale/unverifiable App is skipped for this request only.
2. Call authoritative lease acquire. A 409/full App is skipped for this request only.
3. On success persist `coda:<app_id>#<lease_id>`.
4. On ambiguous transport loss, retry once on the same App with the same owner and lease ID. Accept only the same lease ID. Never continue to another App after an ambiguous outcome.
5. Authentication/configuration errors and malformed successful responses fail the request; they are not capacity signals.

There is no health cache, so stale-health behavior is explicit: no prior health result authorizes selection; every NEW acquisition performs a fresh bounded probe. Restart resets cursor to index 0. Fairness is best-effort within one process lifetime, not durable across restart. Durable fairness is not an approved product requirement.

After acquisition, registry lookup uses only persisted `app_id`. Connect, allocate/adopt, relaunch, terminate and cleanup make exactly one App-targeted call and never use cursor/fallback. Storage is shared within that App and isolated across Apps.

## OPTIONAL resilience enhancements (not MVP)

- Persisted cursor/fairness across restart.
- Health TTL, quarantine/backoff, durable health or capacity history.
- Alias/migration tables for arbitrary App-ID renames.
- Administrative health history, durable pool dashboards, or raw App metadata projections.
- CoDA authoritative `GET /api/omnigent-host/leases/by-idempotency-key/<key>` (or equivalent).

The last item is an explicit dependency on repository `coding-agents-databricks-apps`, requiring endpoint implementation, authorization, tests and version coordination before Omnigent may rely on it. It is not in the Omnigent MVP plan.

## Impact map and complexity budget

### REQUIRED Omnigent changes (one repository)

| Area | Expected files | Budget |
|---|---|---|
| Provider/registry/scheduler | `omnigent/onboarding/sandboxes/coda.py` | 1 |
| Config/factory/lifecycle lookup | `omnigent/server/managed_hosts.py` | 1 |
| Adoption fencing, only if provider routing is insufficient | `omnigent/server/routes/sessions/routes_core.py` | 0–1 |
| Shared-host cleanup, only if provider routing is insufficient | `omnigent/server/routes/sessions/routes_events.py` | 0–1 |
| Provider/config tests | `tests/onboarding/sandboxes/test_coda.py`, `tests/server/test_managed_hosts.py` | 2 |
| Lifecycle tests | existing route/integration tests corresponding to changed routes | 0–2 |
| Databricks environment construction | `deploy/databricks/src/app.py` | 1 |
| Bundle variables/environment | `deploy/databricks/databricks.yml` | 1 |
| Deployment CLI forwarding | `deploy/databricks/deploy.py` | 1 |
| Deployment tests | existing tests for the preceding deployment files | 1–3 |

The current deployment path demonstrably uses singleton `CODA_APP_NAME`/`CODA_APP_URL` in those three named deployment files. MVP changes that input to one serialized pool value (plus the existing public server URL); authentication remains the existing Databricks App authentication, so no new secret resource is required.

**Initial automatic-MVP budget:** repository `omnigent` only, 0 database migrations,
0 CoDA endpoints, and 0 durable scheduler/health tables. The later manual-selection
extension intentionally adds one authenticated Omnigent projection and Web UI changes;
it does not change CoDA or add durable scheduling state.

### Surface disposition

- Store/schema: reuse `hosts.sandbox_id`; no schema change.
- Launch/relaunch/allocation/termination/cleanup: existing paths above, resolve persisted App ID.
- API projections: the authenticated manual-picker endpoint exposes only stable labels,
  immutable App IDs, sanitized ownership, and advisory capacity; lease IDs remain opaque.
- UI: New Session retains the automatic row and adds disabled/available per-App rows for CoDA.
- Observability: structured logs per attempted `app_id`, outcome class, selected App, exhaustion and wrong/removed fence; no durable state or high-cardinality URL/lease metrics.
- CoDA repository: no REQUIRED change. Optional query endpoint is explicitly separate.

## Migration, rollback and removed Apps

Deploy code that accepts both singleton and pool first. Convert singleton to a one-entry pool retaining `app_id = old app_name`, then add entries. Existing records need no rewrite. Keep every configured `app_id` while live/persisted hosts reference it. Rollback removes additional entries and restores singleton only after their leases are terminated; persisted leases are never reassigned. Removed-ID operations fail closed and require restoring that registry entry or manual cleanup against the granting App.

## Deterministic acceptance validation

Use fixed UUIDs, fake App getter/HTTP functions, fixed config order and controlled threads; no Apps/network/resources.

1. Config: singleton normalization; valid pool; empty/partial/duplicate ID/name/URL; legacy+pool rejection.
2. Selection: exact A→B→C starts, wraparound, lock-protected concurrent distinct starts, restart starts at A, and documented non-durable fairness.
3. Availability: fresh probe each NEW request; inactive/not-ready/timeout skip; full 409 skip; all unavailable/full bounded exhaustion; 401/400/malformed success fails rather than falls through.
4. Ambiguity: response loss retries same App/same lease ID once; matching response succeeds; different/unknown response fails and makes zero calls to later Apps.
5. Fencing: interleaved A/B connect, workspace/adoption, relaunch, terminate and cleanup call only persisted `app_id`; renamed display name still resolves by ID; removed ID fails before HTTP.
6. Storage invariant: sessions on A may receive directories under A's shared `coda-sessions`; no B operation or lookup occurs.
7. Persistence/migration/rollback: existing legacy ID works in normalized singleton; one-entry→pool retains ID; removing live ID fails closed; rollback requires terminating added-App leases.
8. Impact gates: assert no DB migration, CoDA endpoint, durable health, or scheduler table enters the diff; API/UI changes stay limited to manual selection.

## Acceptance-criteria audit

- Configuration, duplicate/partial/removed behavior: REQUIRED contract and tests 1, 5, 7.
- Deployment/bundle/environment: impact budget documents YAML-only pass-through and no resource deployment.
- API/store/UI: explicit no-change dispositions with existing `sandbox_id` evidence.
- Launch/relaunch/allocation/termination/cleanup: existing paths mapped and test 5.
- Deterministic distribution/tie-break/concurrency/restart/fairness: process-local locked cursor contract and test 2.
- Stale health/full/request failure: fresh-probe policy and tests 3–4.
- Shared-within-App, isolated-between-Apps: invariant and test 6.
- Migration/rollback: operational identity-preserving strategy and test 7.
- Observability: bounded structured logs only.

**Final recommendation: GO for the bounded REQUIRED MVP; NO-GO for OPTIONAL durable scheduler state or cross-repository endpoint work without separate approval.**

## Manual New Session selection extension

The automatic MVP remains the default and is still represented by an omitted
`sandbox_app_id`. For CoDA deployments, the authenticated New Session picker may
also call `GET /v1/sandboxes/coda` and submit one returned immutable `app_id` as
`sandbox_app_id` on `POST /v1/sessions` with `host_type: managed`.

The list endpoint returns only stable configured-order labels
`CoDA-Sandbox-1`, `CoDA-Sandbox-2`, …, immutable IDs, sanitized ownership
(`unclaimed`, `mine`, `other`), advisory state (`available`, `full`,
`unavailable`), and session capacity. It never returns Databricks App names or
URLs, user identifiers, host/lease IDs, tokens, or raw upstream payloads. App
readiness is freshly probed; capacity remains advisory because CoDA lease CAS is
authoritative at claim time.

A manual claim is authorized with the session creator identity and validated
against the configured immutable registry before a conversation is created. It
attempts exactly the selected App, does not advance the automatic round-robin
cursor, and never spills to another App on unavailable/full/ambiguous/error.
The existing same-owner adoption lock, owner single-flight, CoDA owner-CAS, and
same-lease-id ambiguity replay provide concurrency safety and idempotence.
After acquisition, `hosts.sandbox_id = coda:<app_id>#<lease_id>` remains the
durable fence. Workspace allocation, reconnect, relaunch, disconnect, cleanup,
and removed-ID handling resolve only that persisted App. In particular,
relaunch releases and reacquires on the granting `app_id`; it never invokes
pool fallback. Session workspaces remain distinct under that App's
`coda-sessions` root, while no lifecycle call can cross into another App's
storage.

### Failure, rollout, and rollback

Unavailable and full options are visible but disabled in the UI; an
authoritative race at create time fails on the selected App rather than silently
rerouting. Removed IDs are rejected before session creation, and existing hosts
with a removed ID fail closed until the operator restores the registry entry for
cleanup. The automatic row remains usable and unchanged throughout rollout.

Roll out server/API support before the matching Web bundle. Keep every existing
immutable `app_id` configured while a host may reference it. To roll back the UI,
serve the prior bundle: omitted `sandbox_app_id` immediately restores automatic
behavior. To roll back the pool/server, first delete sessions and release hosts
on added Apps, verify no persisted `coda:<added-id>#…` fences remain, then remove
those entries or restore the legacy singleton. Never rename/reuse an ID or
rewrite a persisted fence to another App.
