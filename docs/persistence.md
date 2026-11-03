# SQLite persistence profile v1

The optional Backpack Store adapter is the first persistent `Store[T]` profile.
The base package retains zero runtime dependencies and `MemoryStore` remains
process-local. This contract selects the existing Backpack Store relational
SQLite and owned asynchronous facade, rather than adding another SQL backend.

## Supported model intersection

Register one trusted dataclass per adapter with an explicit namespace, table,
type key, payload version and string primary-key field. The selected Enforce
Type codec validates on both read and write. Columns support the Backpack
scalar profile (`str`, safe `int`, finite `float`, `bool`, nullable scalars).
Nested collections, UUID columns, relationships and arbitrary API dataclasses
are outside this first profile and fail during explicit registration. Domain
keys use Backpack's bounded ASCII key grammar; storage UUIDs remain separate.
No model is imported from stored data.

## Store semantics

- `create` validates identity equality and fails with API `ConflictError` on
  duplicate keys or declared unique constraints.
- `get`, `list` and transforms return detached validated values. Missing keys
  map to API `NotFoundError`; backend/codec errors never disclose submitted data.
- `list(offset, limit)` uses ascending immutable domain-key order, with the
  existing API bounds (nonnegative offset, limit 1–1000). Each page runs in one
  read transaction; separate pages see current committed state and can shift
  under concurrent insertions/deletions. Offset work is bounded at 10,000.
- `update` fetches the current row and runs the synchronous transform inside one
  `BEGIN IMMEDIATE` unit of work, then replaces using its current revision.
  Transform exceptions or invalid output roll back without changing the row.
  Transforms must avoid external side effects and recursive storage calls.
- `delete` reads and deletes in one write transaction. Revisions are canonical
  decimal strings and increment on replacement. The adapter exposes an explicit
  checked update for application-level optimistic concurrency; ordinary CRUD
  preserves the existing unconditional atomic-transform contract.

## Ownership, durability and recovery

Open only during application startup. One async facade owns one worker thread,
one SQLite connection, one event loop/process and a bounded admission queue.
Close stops admission, drains accepted work and joins the owning worker. Do not
share an adapter across processes or event loops. SQLite WAL is the selected
one-host/local-filesystem profile; independent processes serialize writers using
SQLite. There is no network-filesystem or multi-host guarantee.

Before dispatch, cancellation prevents an operation. After dispatch, Backpack's
`DispatchedCancellation` retains an outcome receipt: reconcile the receipt or
reload before retrying a write. A canceled HTTP request does not imply rollback.
Queue/busy/closed/backend failures stay typed internal storage failures rather
than claiming a successful write. No events are published by this adapter.

Explicit initial schema registration is supported. Existing unknown schema
versions or changed registrations fail closed; migrations remain Backpack-owned
and must be supplied through its migration facilities before opening this
profile. Never silently rebuild or overwrite an existing database. Stop services,
retain a verified complete database backup and compatible code/dependency
revisions before an upgrade. Restore that checkpoint with matching code.

## Qualification

The implementation must run shared detached-value/CRUD/transform conformance,
reopen persistence, invalid output rollback, checked-write conflicts, canceled
dispatch and independent-process conflicting transforms. Backpack's migration,
backup and process tests remain the owning backend evidence. Because the new
interfaces are unreleased, integration must pin an immutable development
revision and label it explicitly; the published `backpack-store==0.1.0` alone
does not provide this profile. Publication is a separate decision.
