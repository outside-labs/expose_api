# Resource event profile v1

This profile adds explicit, read-only, single-record subscriptions to native
ASGI. It uses only the standard library. Collections, client writes, arbitrary
filters, historical value replay and delivery acknowledgments are outside v1.
The HTTP development runner does not perform WebSocket upgrades.

## Explicit registration

```python
from dataclasses import dataclass, field
from expose_api import App, MemoryStore, Principal

@dataclass
class Note:
    id: str
    owner: str = field(metadata={"expose_api": {"write_only": True}})
    title: str = ""

# Supply a bounded, nonblocking verifier for the application's credentials.
app = App(authenticate=verify_credentials, auth_challenge="Custom")
notes = app.include(Note, store=MemoryStore(events=256), path="/notes",
    authorize=lambda request: "read" in request.principal.scopes,
    record_policy=lambda principal, operation, value: principal.subject == value.owner)
app.subscribe(notes, origins=("https://ui.example",), hosts=("api.example",),
    connections=64, pending_limit=64, poll_interval=0.25,
    send_timeout=5, auth_timeout=5, subscribe_timeout=5,
    max_message_bytes=1024 * 1024, lifetime=3600)
```

The selected origins/hosts are deployment configuration; the example does not
start a listener. The ASGI host must supply trusted scheme/path/header scope
values. Forwarded headers require separately configured trusted proxy handling.
Base imports open no threads, listeners or stores. `MemoryStore(events=0)` is
the compatible default; enabling subscriptions on it fails at registration.

## Wire contract

The client connects to an existing get-enabled record path, offering the
`expose-api.events.v1` subprotocol. Registration must explicitly select an event
store, exact permitted Origin values and exact Host values including ports.
Missing, duplicate, `null`, wildcard or unlisted origins/hosts fail closed.
Credentials belong to the application's existing authenticator; query strings
and subprotocols must never contain credentials. Browser consumers need an
application-owned cookie verifier or a separately selected credential bridge;
the browser WebSocket API cannot set an Authorization header.

After acceptance the client sends one UTF-8 text message:

```json
{"v":1,"type":"subscribe","cursor":null}
```

`cursor` may instead be a previous 32-character lowercase hex token. Extra or
duplicate fields, binary messages, malformed JSON, non-finite numbers and other
versions/types are refused. A connection owns exactly one record; subsequent
client messages are refused. Messages and outgoing JSON have finite byte bounds.

The server always starts with a fresh authorized snapshot, using exactly the
resource's HTTP output projection (including nested write-only field removal):

```json
{"v":1,"type":"snapshot","cursor":"00000000000000000000000000000001","value":{"id":"example","title":"Saved"}}
```

Snapshots replace the client's remote baseline. Preserve pending edits
separately and resolve conflicts through the existing REST workflow. A snapshot
can coalesce multiple committed writes and may repeat a value. It is not a
historical change payload, an exactly-once stream or a conditional-write token.
Cursor equality permits duplicate suppression; cursors are opaque, unordered
and distinct from domain/storage revisions, UI render generations and schema
versions. Client generations must reject completions from an earlier connection.

An unknown/expired cursor or an exceeded pending-change budget produces a
value-free `{"v":1,"type":"resync"}` followed by a fresh snapshot. Reconnect
with the last cursor or null. Fresh state, rather than historical replay,
recovers missed updates and process restarts. Missing and denied records have
the same close behavior (1008); no tombstone or prior owner payload is delivered.
Deletion and recreation may coalesce to the newly authorized current value.

## Authorization and lifecycle

Subscriptions require a protected resource with a configured authenticator,
operation authorization and synchronous record policy. Public and legacy
operation-only mounts do not implicitly enable events. Admission and every
poll re-run the authenticator and `get` operation authorization, bind a fresh
principal, read an atomic value/cursor snapshot, and apply the current `get`
record policy before output projection. A changed principal closes the socket;
revocation, inaccessible/missing rows and operation denial close generically.
No prior event value is used to authorize current delivery. Policy/authenticator
failures are redacted; cancellation propagates. A concurrent change after the
authorized snapshot is possible, as for an HTTP read. Application callbacks must
remain bounded and nonblocking; async authentication has an explicit timeout.

There is one bounded poll and at most one bounded send per connection, with no
unbounded per-subscriber queue. Registration bounds connections, retained
changes, pending changes, message bytes, poll interval, send/authentication and
initial-message timeouts, and total connection lifetime. A slow sender closes
1013; invalid input/access closes 1008; oversized messages close 1009; unexpected
failures close 1011; shutdown closes 1001. Before acceptance, refusal is a
generic WebSocket close (ASGI maps it to handshake denial). Disconnects and
failed sends release admission and owned tasks. Lifespan shutdown stops
admission, cancels/drains subscriptions, then closes application-owned stores.
An App is owned by one process/event loop while its subscriptions run.

## Storage and cross-process delivery

An optional `EventStore` exposes atomic `event_snapshot(key)` and
`event_changes(cursor, key=..., limit=...)`. Snapshots pair detached current
values with the journal head in one transaction. Changes contain opaque tokens
and keys only, never credential, principal or model payloads. A store write and
its journal append commit or roll back together, including policy/validation
failures. Storage success precedes delivery; cancellation after dispatch can
still commit, with the existing receipt/reload reconciliation rules.

MemoryStore journals are opt-in, bounded and process-local. Restart discards the
stream. The persistent adapter uses explicit registered journal/state models
in the same SQLite database and unit of work as the resource mutation. All
writers for that resource must use the journal-enabled profile. Separate
processes poll committed journal state; a shared database alone is insufficient.
Retention is a finite change count, not a time guarantee. Lagging subscriptions
resync. A journal outage fails its write transaction; there is no successful
write followed by a best-effort in-memory publish. Direct SQL or uninstrumented
writers are outside the profile. Schema compatibility and enabling events on an
existing database require the adapter's explicit upgrade procedure.

The initial deployment profile remains one host/local SQLite database, bounded
writers and process-local sockets. No broker, hosted database, external identity
provider, package publication or public deployment is selected here.

## Persistent journal selection

Open `BackpackStore` with its usual model/path/namespace/table/type key plus
`events=256, event_namespace="note_events"`. The event namespace is a distinct
explicit lowercase identifier (at most 40 characters). It owns
`app_note_events_state` and `app_note_events_changes` using journal schema v1.
Every worker must select the same retention, resource table/type key and event
namespace; incompatible startup fails closed. The resource schema and existing
payload/revision contracts do not change. Read transactions currently use the
public write unit of work to pair resource and journal state, so polling must
be bounded to fit the single-host SQLite writer budget.

To enable events on existing data, stop all writers, make the application's
verified backup, start all writers with the event profile, and reconnect clients
for fresh snapshots. Creating the separately owned journal namespace does not
rewrite resource rows. Older code can still read the resource schema, but must
not write after enablement: it does not append journal entries. To roll back,
stop event-enabled workers and clients before restoring the prior application
profile; restart clients with fresh snapshots when re-enabling events. Do not
delete journal tables or change retention while writers are running.

The journal rotates a fixed set of registered slots, preserving at most
`events` change rows and `events + 1` journal key reservations. No historical
record payloads are stored. Opaque cursors survive graceful worker restart.
Backpack's existing permanent domain-key reservation means a deleted resource
key cannot be recreated; MemoryStore permits that case. The stream position has
a finite exact-integer bound and fails writes on exhaustion rather than wrapping.

Reference: [ASGI WebSocket specification](https://asgi.readthedocs.io/en/stable/specs/www.html#websocket).
