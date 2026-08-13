# Authenticated callback idempotency

`POST /v1/sessions/{session_id}/events` accepts an optional top-level
`idempotency_key` for the durable `external_conversation_item` callback.
Transient callbacks and other event types reject the field so callers cannot
mistake a non-durable acknowledgement for a deduplicated write.

## Wire contract

- **Key:** 1–128 ASCII characters matching
  `[A-Za-z0-9][A-Za-z0-9._:-]*`. A producer creates it before its first network
  attempt and retains it across authentication refresh, transport ambiguity,
  process restart, and future outbox replay. Pi uses
  `pi:v1:<base64url-HMAC-SHA256>` over the canonical callback envelope, keyed
  by its persisted session relay secret; legacy configs without that secret
  omit the key rather than emit a predictable one.
- **Scope:** the key is bound to the workspace, authenticated caller, target
  session, event type, and canonical request payload. Only SHA-256 digests of
  the key, caller, and payload are persisted.
- **First write:** the callback item and idempotency record commit in one
  transaction under the conversation position lock. Concurrent first writes
  have exactly one durable effect.
- **Replay:** an identical replay returns the original `item_id`, republishes
  that already-persisted item to reconcile commit-before-response/process loss,
  and never appends again. Reuse with a changed payload, authenticated caller,
  target session, or event type returns the same generic client error so the
  existing binding is not disclosed.
- **Retention:** records are retained for seven days. Each keyed write and a
  minute lifecycle task remove at most 100 expired records per batch, bounding
  work while reclaiming idle keyspaces. Reuse after expiry is a new operation;
  an outbox must not replay past this window.
- **Telemetry:** successful replays emit one low-cardinality log containing
  only the event type. Keys, caller identity, payloads, and digests are not
  logged.
- **Legacy clients:** omitting `idempotency_key` preserves append-on-every-call
  behavior.

The migration is additive and rollback drops only the deduplication table.
Rolling back restores legacy duplicate-prone behavior and does not remove any
conversation item already committed while the table existed.
