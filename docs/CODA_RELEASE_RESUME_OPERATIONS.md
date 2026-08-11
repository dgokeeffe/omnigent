# CoDA history-preserving Release and Resume operations

## User-visible lifecycle

**Release session** retains the conversation and detaches only that session. A
shared lease is not scrubbed while another session remains. **Release sandbox**
detaches every session on the authenticated owner's claim and then disconnects
and scrubs the exact persisted App lease. Both actions may permanently erase
sandbox files and uncommitted changes.

**Resume** is explicit. It acquires automatically or uses the selected available
CoDA target, reconstructs the retained repository URL/base branch into a fresh
per-session workspace, and leaves the session detached if any step fails.
Clients never submit host or lease IDs.

## Health and incident triage

1. Confirm the conversation remains readable and reports `detached: true`.
2. Query `GET /v1/coda/claims` as the affected owner. The response is sanitized;
   use database/operator tooling, not user-visible payloads, to correlate its
   anchor session with a host row.
3. If Release returns `503`, treat disconnect as ambiguous. Do **not** delete the
   host row, clear `sandbox_id`, or edit the encoded App/lease fence. Retry the
   same Release endpoint; it resolves the cleanup-only detached claim link and
   addresses the same persisted fence.
4. If Resume returns `409`, verify the chosen App remains configured, available,
   owner-authorized, and below capacity. Manual Resume does not fall through to
   another App. Retry automatic acquisition or select a currently available
   target. The conversation must remain detached until binding succeeds.
5. Never include App names/URLs, raw owner identities, host or lease IDs, tokens,
   credentials, or upstream response bodies in tickets, screenshots, or logs.

## Safe rollback

1. Disable or route away new Release/Resume requests and wait for in-flight
   operations to settle.
2. Back up the database. Inventory host rows with `sandbox_provider='coda'` and
   metadata rows with `detached_claim_host_id` set.
3. Reconcile every cleanup-only link by retrying Release against the persisted
   fence. If provider status remains ambiguous, retain both host row and link;
   escalate to provider tooling without changing automatic routing.
4. Roll back application code. Detached sessions are ordinary retained,
   unbound conversations to older code. They do not need transcript migration
   and must not be deleted to complete rollback.
5. Only after all cleanup links are clear may the reversible migration be
   downgraded. Downgrading drops `detached_at`, `detached_claim_host_id`, and
   their indexes; it never reconstructs a destroyed workspace.
6. Re-enable traffic and verify normal automatic/manual new-session acquisition.

Do not use a live-session delete as a Release substitute, do not deploy a
rollback while ambiguous fences are unresolved, and never mutate a different
owner's host to recover capacity.
