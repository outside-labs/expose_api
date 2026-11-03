# Identity and record access profile v1

This profile adopts caller-owned identity verification and synchronous record
policies for native HTTP resources. It adds no credential database, identity
provider, token issuer, session service or runtime dependency. An application
selects and implements its verifier before serving sensitive data.

## Identity and credentials

`Principal(subject, tenant=None, scopes=frozenset())` is an immutable context of
bounded opaque strings. Subject and tenant identifiers have no implied database
lookup, URL meaning or authority. Scopes are application-defined. Only the
configured authenticator may produce the principal; incoming scope fields,
headers and JSON values never become trusted identity implicitly.

The application supplies `App(authenticate=...)`. Its sync/async callback receives
the request and returns a `Principal` or `None`. It owns credential validation,
expiry, revocation, audience/issuer checks and its own I/O bounds. It must not
block the event loop. A protected request resolves identity exactly once before
operation authorization or body consumption. Invalid return types and callback
failures are redacted internal errors; cancellation propagates. `None` produces
401. An explicit validated challenge is required when configuring authentication
so the response can include the application's `WWW-Authenticate` scheme.

Credential transport belongs to the application. Bearer credentials require TLS
outside loopback; do not put tokens in URLs, bodies, logs or model fields.
Cookie credentials additionally require a selected CSRF/origin contract. No
header, cookie or forwarded identity is trusted by default. This is an identity
integration seam, not an OAuth implementation.

## Registration and operation authorization

Existing `public=True` mounts and existing operation-only `authorize(request)`
mounts retain their behavior. A protected mount explicitly selects a
`record_policy` as well as `authorize`; it requires a configured authenticator.
Its operation callback receives `Request.principal` and must return an exact
boolean. Denial returns 403 before reading the request body or accessing storage.
HEAD follows GET policy. OPTIONS exposes only the selected method allowlist and
does not authenticate, read storage or run policy callbacks.

## Record policy and atomic storage

`Resource.for_principal(principal, policy, scan_limit=10000)` creates a separate
request-owned resource view. Policy receives `(principal, operation, value)` and
returns an exact boolean. It is synchronous, pure, bounded and sees a detached
copy including fields omitted from HTTP output. It must not perform I/O or
recursive storage calls. Async results and non-booleans fail closed.

- Create checks the validated new value before storage admission. Denial is 403.
- Get hides a policy-denied value as 404, using the same response as a missing key.
- List filters before the visible offset and limit, in one storage snapshot.
  At most `scan_limit` records are inspected. Exhaustion returns a value-free 503,
  never a partial page or hidden-row count. Pages across requests can shift.
- Update checks both current and proposed values inside the atomic transform.
  Inaccessible current values are 404; forbidden proposed values are 403. A
  denied or failing transform leaves the row and revision unchanged.
- Delete checks the current value inside the same transaction as deletion.
  Inaccessible values are 404; no fetch-then-unconditional-delete fallback exists.

Protected views require the additional `AccessStore` methods `list_where` and
`delete_where`. Unsupported stores fail at configuration time. MemoryStore and
the optional Backpack adapter implement these capabilities. Field visibility,
identity immutability, validation, checked revisions and accepted-write
cancellation retain their owning contracts. A direct unbound Resource remains
trusted Python access and is not an HTTP authorization boundary.

Tenant isolation is an explicit policy comparing the trusted principal with
stored/proposed tenant ownership. Clients must not be able to move records to
another tenant. Domain keys are global within a store; create can return a
generic 409 for an existing key or unique constraint, including an inaccessible
record. Applications requiring concealed key existence must allocate opaque
keys or separate tenant stores/namespaces and constraints. This profile does
not claim storage-enforced row security or equivalent timing for denied rows.

## Errors, documentation and delivery

HTTP errors contain fixed messages and value-free issue paths/codes. Policies,
principals and credentials never appear in generated OpenAPI. Documentation
marks protected operations and their actual 401/403/404/503 behavior without
inventing an OAuth flow. Verifier failures never turn into anonymous success.

This contract covers HTTP. WebSocket subscription authorization, revalidation
and commit-before-delivery remain WS-01/WS-02 work. Authentication does not select
a hosting service, publication or deployment.

References: [ASGI HTTP](https://asgi.readthedocs.io/en/latest/specs/www.html),
[Bearer transport requirements](https://www.rfc-editor.org/rfc/rfc6750.html).
