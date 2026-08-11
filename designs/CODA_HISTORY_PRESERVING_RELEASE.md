# History-preserving CoDA release and resume

Status: accepted for the first release milestone.

## Contract

A **detached session** is a retained conversation whose operational binding is
absent: `host_id`, `runner_id`, live `workspace`, `git_branch`, terminal/runtime
state, and live-status fields are cleared. Its transcript, title, archive state,
permissions, comments, labels, files, project membership, native session id, and
repository reconstruction metadata remain. Repository reconstruction metadata
is a server-owned snapshot of the repository URL and requested base branch; it
is never interpreted as proof that old workspace files survived.

A **CoDA claim** is the persisted managed-host row containing the opaque CoDA
sandbox fence. The fence encodes the granting App and lease and is never accepted
from a client or returned by an API. Inventory is derived from caller-owned host
rows and owner-level session grants. Public responses contain only session IDs,
titles, detached state, and affected-session counts.

Release may permanently erase sandbox files and uncommitted changes. Every API
description and confirmation UI must say so. Release never deletes conversation
history or retained metadata.

## State transitions

| Current / event | Transition and result |
|---|---|
| Bound session / Release session, no sibling on claim | Stop/coordinate its turn; atomically detach it; disconnect and scrub the exact persisted CoDA fence; delete the host row only after definitive disconnect; `204`. |
| Bound session / Release session, sibling remains | Atomically detach only the selected session; do not disconnect, scrub, or delete the shared claim; `204`. |
| Bound session / Release sandbox | Under the claim lock, atomically detach every session currently bound to that host, then disconnect/scrub exactly its persisted fence once. Delete host only after definitive success; `204`. |
| Detached session / either Release | No mutation and no provider call; `204` (idempotent). |
| Archived bound session / Release | Same as bound. Archive state remains true. |
| Sub-agent session / Release | Rejected `409`; release the root session or sandbox so a child cannot split lifecycle ownership. |
| Failed launch without a durable claim / Release | Detach any partial operational binding, preserve history and reconstruction metadata; `204`. |
| Running turn / Release | `409 SESSION_BUSY`; caller retries after stop. Release never races a live turn. |
| Release concurrent with create/adopt/relaunch | Serialized per durable host/claim. The loser re-reads state. No acquired operation may fall through to another App. |
| Disconnect definitively succeeds | Host row/fence may be removed. |
| Disconnect fails or outcome is ambiguous | Return sanitized `503 RELEASE_UNCERTAIN`; preserve the host row, opaque App/lease fence, and recoverable host state. Sessions stay detached. A retry addresses the same fence. |
| Missing/removed configured App during release | Use the persisted App fence, not current automatic routing. If no safe launcher can target it, `503 RELEASE_UNCERTAIN`; preserve fence. Never reroute. |
| Detached session / Resume automatic | Acquire using existing automatic pool behavior, allocate a fresh per-session workspace, reconstruct the retained repository/base branch, and bind only after success. |
| Detached session / Resume manual | Validate the requested configured App is visible and available to the caller, then acquire only that App. No client lease/host input is accepted. |
| Bound session / Resume | `204` with no acquisition (idempotent). |
| Resume concurrent with release/resume/create | Serialized on session and selected/new claim. Exactly one binding wins; retries re-read it. |
| Resume target removed/full/unavailable | Sanitized actionable `409 SANDBOX_UNAVAILABLE`; session remains detached. No fallback after manual selection. |
| Resume acquisition/reconstruction/bind failure | Best-effort cleanup of the newly acquired claim. Atomically restore/retain detached state; `503 RESUME_FAILED`. Never restore stale workspace affinity. |
| Archive/unarchive | Changes visibility only; does not release, resume, or alter runtime affinity. |
| Delete | Existing destructive lifecycle remains distinct. It deletes conversation data and may clean resources; UI must not present it as Release. |

## Atomicity and fencing

Detach is a compare-and-update transaction over metadata rows selected by host
ID and workspace partition. Host lookup is owner-scoped. A host-leading metadata
index backs count/list-by-host; no full conversation scan is permitted. Detach
clears runner/host/workspace/branch, live-status and terminal launch affinity in
one transaction while leaving retained fields untouched.

In-process locks serialize this tranche. Cross-replica serialization is
explicitly deferred to `omnigent-0hd.9.8`; database compare-and-update guards
still make repeated requests and process-separated races safe rather than
cross-routing. Provider operations always resolve the launcher from the
persisted sandbox provider and App fence. Automatic routing is used only for a
new automatic acquisition.

## Authentication, authorization, and disclosure

All inventory, release, and resume endpoints require authentication. Session
operations require owner-level permission; a missing or foreign session returns
the same `404`. Claim inventory and sandbox release include only claims whose
host owner equals the authenticated principal and whose affected sessions are
owner-accessible. Logs and errors use session IDs or redacted stable digests;
they must not emit raw owner identity, App name/URL, host/lease IDs, tokens,
credentials, or upstream bodies.

Clients may supply only a session ID and, for manual resume, the existing public
App selector ID already used by new-session acquisition. They never supply a
host ID or lease ID.

## API and UI

* `GET /v1/coda/claims` returns sanitized caller-owned claim inventory.
* `POST /v1/sessions/{id}/release` returns `204` when detached/already released.
* `POST /v1/coda/claims/{session_id}/release` identifies a claim by an owned
  member session (not host or lease), releases all affected sessions, and
  returns `204`.
* `POST /v1/sessions/{id}/resume` accepts optional `{ "sandbox_app_id": "…" }`
  and returns `204` after binding. Omission preserves automatic acquisition.

Release confirmations list affected session titles/IDs and state: “Conversation
history is kept. Sandbox files and uncommitted changes will be permanently
erased.” Controls expose keyboard focus, accessible names, cancellation,
releasing/detached/retryable/already-released states, and refresh inventory and
session state after every mutation.

## Operator rollback

The feature is additive. Before rollback, stop new release/resume traffic and
wait for in-flight operations. Detached rows remain valid retained
conversations on old code (unbound sessions); no schema downgrade may repopulate
stale affinities. Ambiguous claims must be reconciled against their persisted
App fence before manually removing host rows. Never clear a fence merely to
make a retry disappear.
