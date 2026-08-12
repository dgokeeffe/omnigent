# CoDA managed-host selection: findings and backlog

Read-only investigation of how `host_type: "managed"` sessions pick a CoDA App
and lease, single-user and multi-user. No product code was changed; every claim
below was confirmed by reading the cited code and by running the repo's CoDA
tests plus throwaway harnesses (deleted afterwards). Behaviour recorded against
`omnigent/server/routes/sessions/routes_core.py`,
`omnigent/server/routes/sessions/routes_lifecycle.py`,
`omnigent/server/routes/hosts.py`, `omnigent/server/managed_hosts.py`,
`omnigent/onboarding/sandboxes/coda.py`.

Design of record: `designs/MULTI-CODA-POOL.md`. Operations:
`docs/CODA_RELEASE_RESUME_OPERATIONS.md`.

## How selection actually behaves

### Automatic mode (`sandbox_app_id` omitted)

1. Before provisioning anything, create lists the caller's own hosts
   (`host_store.list_hosts(owner)`, most recently active first) and keeps the
   ones that are CoDA-backed, have a `sandbox_id`, and are live
   (`host_is_live` = `status == "online"` **and** last seen within
   `HOST_LIVENESS_TTL_S`).
2. It adopts the first candidate whose bound-session count is below
   `sandbox.coda.max_sessions_per_lease` (default
   `CODA_DEFAULT_MAX_SESSIONS_PER_LEASE = 10`). Adoption calls
   `allocate_workspace` for a fresh per-session directory under that App's
   `coda-sessions` root and binds the session to the same `host_id` — same App,
   same container, shared storage.
3. With no adoptable host it starts one owner-scoped launch
   (`app.state.coda_owner_launches[owner]` single-flight) which calls
   `provision(host_name)` with no `app_id`: round-robin over the configured pool
   (`CodaPoolState.reserve_start`), fresh readiness probe per candidate, then the
   authoritative lease acquire. A candidate is skipped on `unavailable`,
   `409 full`, or `existing-owner-lease`; ambiguity replays once on the same App.
4. The granting App is persisted as the durable fence
   `hosts.sandbox_id = coda:<app_id>#<lease_id>`; every later connect, workspace,
   relaunch, terminate and cleanup resolves only that `app_id`.

So the intuition "once you claim a CoDA, later sessions ride it" is correct, with
three qualifiers: **per user**, **only while the host is live**, and **only up to
the per-lease session cap**; past the cap the next session acquires a new lease on
the next App in the pool.

Observed with a 2-App pool and cap lowered to 2 (owner-CAS modelled as the real
endpoint behaves):

```
1) first automatic create acquired coda:app-1#...
2) second automatic create adopted the SAME host (no second acquire)
3) cap reached -> new acquire, round-robin lands on app-2
4) next create adopts whichever live lease still has room
5) all own leases at cap -> "CoDA pool has no available lease capacity
   (app-1=existing-owner-lease, app-2=existing-owner-lease)"
6) deleting a session frees a slot -> next create re-adopts the first host
7) all own hosts offline -> same existing-owner-lease exhaustion
```

### Manual mode (`sandbox_app_id` set)

Validated against the immutable registry before the conversation is created
(unknown id → `409` "removed or unavailable"), attempts exactly that App, never
advances the automatic cursor, and never spills on unavailable / full /
ambiguous / error.

### Multiple users

Everything is keyed on the authenticated user as lease owner (`owner = user_id`,
`"local"` only when auth is disabled):

| Layer | Scope |
|---|---|
| Adoption candidates | `list_hosts(owner)` — you can only adopt your own hosts |
| CoDA lease | one active lease per App; owner-CAS for the holder, `409` for anyone else (omnigent reads that as `full` and skips) |
| Launch single-flight | `coda_owner_launches[owner]` — per user |
| Adoption critical section | `coda_adoption_lock` — one **process-wide** lock |
| Picker `GET /v1/sandboxes/coda` | per user, sanitized `mine` / `other` / `unclaimed` |

Consequence: **pool size ≈ maximum concurrent users**, each pinning one App and
up to `max_sessions_per_lease` sessions inside it. Observed with alice/bob/carol,
2 Apps, cap 2:

```
A) alice acquires app-1
B) bob (cursor forced to start at app-1): app-1=409-full -> acquires app-2;
   never adopts alice's host
   picker[alice]: app-1 mine/available used=1 | app-2 other/full used=None
   picker[bob]:   app-1 other/full used=None | app-2 mine/available used=1
C) bob's next automatic session adopts BOB's host
D) carol automatic create -> "no available lease capacity (app-2=full, app-1=full)"
E) carol manual pick of app-1 -> "(app-1=full)" — no spill
F) carol create with alice's host_id -> 403 "not your host"
G) alice's 2nd session adopts alice's host
H) alice's 3rd session -> "(app-1=existing-owner-lease, app-2=full)"
```

Sessions shared through permission grants still execute on the owner's claimed
CoDA — a collaborator's turns run in the owner's workspace. That is the intended
sharing semantic, not a leak of another user's claim.

## Backlog

### 1. Cap counting reads a single page (correctness)

`routes_core.py` counts sessions per host from
`conversation_store.list_conversations(limit=1000, kind=None)`. Beyond 1000 rows
the count undercounts, so a lease can be filled past
`max_sessions_per_lease`. The picker path (`hosts.py`) already paginates fully.

Fix: paginate the same way, or add a store-side count-by-host query — that also
removes a 1000-row fetch from the hot path of every managed create. Regression
test: >1000 conversations, cap 1, assert the second create does not adopt.

### 2. Dead claim wedges automatic create while the picker says "available"

Automatic create skips non-live hosts and attempts a fresh acquire; CoDA's
owner-CAS returns the caller's still-active lease, so the App is skipped as
`existing-owner-lease` and, with a small pool, the create fails. The picker
deliberately counts only live own hosts, so the same App renders `available`.
Each half is defensible; together the UI offers a click that cannot succeed.

Options: route automatic create through the persisted-fence relaunch/resume path
when the owner's only candidate on that App is a dead row, or project a
`needs_resume` state so the UI points at Release/Resume
(`docs/CODA_RELEASE_RESUME_OPERATIONS.md`) instead of "available".

### 3. App-wide adoption lock wraps a provider HTTP call (throughput)

`coda_adoption_lock` is one process-wide `asyncio.Lock` held across
`allocate_workspace` (a CoDA control request), so concurrent managed creates from
*different* users serialize behind each other's workspace allocation. The
single-flight map is already per owner; per-owner locks would preserve the
invariant (one claim per owner, no duplicate lease) and drop the cross-user
coupling. Needs care: the lock also guards capacity read → adopt/launch decision,
so per-owner locking must keep that decision atomic per owner.

### Deliberate — do not "fix"

- Owner-scoped stickiness up to the cap, and `403 not your host`: the isolation
  contract (sessions may share storage within one App, never across Apps or
  across owners).
- One lease per App, so pool size bounds concurrent users; no spill for manual
  picks; capacity advisory only (lease CAS is authoritative).
- Process-local round-robin cursor reset on restart, best-effort fairness within
  one process lifetime. Durable fairness/health was explicitly NO-GO in
  `designs/MULTI-CODA-POOL.md`.
- Picker redaction: no App names/URLs, owners, host ids, lease ids, or raw
  upstream payloads; `capacity.used: null` for another user's App.

## Reproducing

```bash
cd projects/omnigent
python -m pytest tests/server/integration/test_host_session_binding.py -k coda -q
python -m pytest tests/server/routes/test_coda_release_resume.py -q
```

The first covers adoption of one host by two sessions and the concurrent
first-session single-flight; the second covers cross-user concealment,
owner-scoped inventory, fail-closed manual targets, and release/resume fencing.

Against a live deployment: create a managed session with `sandbox_app_id`
omitted, watch `GET /v1/sandboxes/coda` report `ownership: "mine"` with
`capacity.used` climbing on one App, confirm `host_id` stays identical until
`used == limit`, then confirm the next session lands on `CoDA-Sandbox-2`. With a
second user, confirm their view shows the first App as `other` / `full` with
`used: null` and that their create claims a different App.
