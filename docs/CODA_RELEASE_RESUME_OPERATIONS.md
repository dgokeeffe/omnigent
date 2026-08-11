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

## Repository reconstruction protocol

Repository sessions use CoDA workspace protocol version 2. Omnigent parses the
retained `<repository>[#<branch>]` value on the server and forwards only the
validated credential-free URL, optional branch, and URL-derived repository
name. The first claim carries the durable session ID on the connect request;
adoption and Resume carry the same fields on workspace allocation. CoDA returns
the absolute cloned repository directory, which becomes the session workspace.
A repository response without `workspace_protocol_version: 2` and
`repository_materialized: true` is rejected as an explicit compatibility error;
an empty parent directory is never accepted as clone success.

Private GitHub repositories require a least-privilege `GH_TOKEN` attached to the
CoDA App through its Databricks secret resource. CoDA's credential helper reads
that secret in-process. Never place a token in a repository URL, session label,
request log, error, ticket, or evidence artifact.

Deploy CoDA endpoint compatibility first and verify both CoDA Apps are healthy,
then deploy Omnigent. Roll back in the reverse order: pause new repository
creates/Resume, let in-flight allocations settle, roll back Omnigent, and leave
CoDA v2 in place until no new Omnigent depends on it. A mixed pair must fail a
repository request explicitly rather than silently allocating an empty
workspace. Non-repository session creation remains backward compatible.

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
5. If repository creation or Resume returns a clone/protocol error, verify CoDA
   v2 is deployed first, the requested branch exists, and the App has an
   authorized `GH_TOKEN`. CoDA removes clone-owned partial content; Omnigent
   releases an allocation if the later durable bind fails. Do not manually
   delete the shared claim or a sibling workspace to retry one failed session.
6. Never include App names/URLs, raw owner identities, host or lease IDs, tokens,
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
