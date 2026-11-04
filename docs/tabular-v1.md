# Opt-in tabular HTTP profile, version 1

This adopts [tabular/1](https://github.com/wispsy/compose_application/blob/d8c4e3a1d4cd1979222c0dddcbf0a14d44ee415c/docs/tabular-v1.md).
The finite requests/errors in `tabular-v1.json` accompany the implemented
schema/query and explicitly enabled checked cell routes. Existing
CRUD routes and shapes stay unchanged. A trusted application explicitly
registers a tabular profile on an already included Resource, with declared
column IDs/field mappings, table/schema IDs and a stable application scope.
The store must support registered scalar queries; arbitrary memory/SQL sources
are not silently scanned or inferred.

## Wire surface and admission

Relative to the selected resource mount:

| Route | Operation | Result |
| --- | --- | --- |
| `GET /_table/schema` | list authorization | Public visible column schema/capabilities, table/schema identity and limits |
| `POST /_table/query` | list authorization | Detached projected rows, each with separate durable key and decimal-string revision, request generation and optional cursor |
| `POST /_table/cell` | update authorization, explicitly writable columns | Checked single-cell result with committed projected row |

HEAD follows GET; OPTIONS discloses only enabled methods. Every route rejects
all URL query parameters, including duplicate or malformed encodings. Query
descriptors use strict JSON objects, with duplicate keys, extra fields,
nonfinite numbers, invalid UTF-8 and malformed shapes refused. Request bodies
are at most 65,536 bytes; responses at most 4,194,304 bytes. No JSONP or HTML
interpretation occurs. Unsupported routes/methods keep existing redacted errors.

Schema fields are `contract='tabular/1'`, `table_id`, `schema_version`, `columns`
and `limits`. Each visible column exposes `id`, `label`, `type`, `nullable`,
`visible`, `sortable`, `searchable`, `writable`; internal field mappings and
hidden columns are absent. The first profile supports exact text, Boolean,
portable integer, finite float and explicitly nullable null only. The declared
model/column codec is validated before storage opens or HTTP requests begin.
Nested values, Decimal/date/datetime/formulas require another qualified codec.

Query bodies admit `generation` (required nonnegative portable integer),
`columns` (optional ordered unique visible IDs), `order` (up to three
`{column_id, descending}` objects), `search` (null or `{fields, term}`),
`filters` (up to 16 `{column_id, operator, value}` objects), `limit` (1–1,000,
default 50) and `cursor` (null or at most 16,384 characters). Omitted fields
select all visible columns, no explicit order/search/predicates or cursor.
Up to 64 columns/16,000 cells may be projected. Filter operators retain the
public storage vocabulary eq/ne/lt/le/gt/ge/in/is_null and exact scalar rules.
Filtering/sorting/searching hidden or undeclared fields is refused before SQL.

Literal search uses 1–8 distinct visible searchable text columns and at most
256 Unicode code points, case-sensitive with no normalization. Empty term
clears search. Field order canonicalizes; percent, underscore, backslash,
quotes, NUL and SQL-like input remain literal. Nulls sort last in either
direction, with ascending registered domain key resolving ties. Columns default
to writable=false; explicitly select writable=True to enable checked editing.

## Access, cursors, work and lifecycle

Reuse App's caller-owned credential verifier and exact operation authorization.
Protected schema/query requests authenticate before consuming bodies or
accessing storage. Record policy uses current principal and the list operation
before filling visible pages; denied rows and hidden fields/counts are omitted.
Schema capabilities describe only application-declared visible fields and do
not grant access. Protected responses, including errors, carry no-store.

Cursor identity includes normalized query, schema, dataset, stable configured
application scope and current principal subject/tenant/scopes. Every request
reauthorizes; continuations are untrusted data, never credentials. Changed
query/scope/schema is invalid_cursor. Page size and request generation may
change independently. Refresh discards continuation. Concurrent writes may
move/duplicate/omit rows between pages; no frozen multi-page snapshot is promised.

At most 10,000 policy candidate rows and 1,000,000 approximate SQLite VM
instructions per underlying query are admitted. Applications may lower limits.
Do not describe limit as a scan bound or return an incomplete visible page on
exhaustion. Denied rows do not consume visible slots. Owned AsyncApplicationStore
execution keeps synchronous SQL off the event loop; pages are detached before
return. Queued cancellation skips dispatch; dispatched cancellation can finish
under its storage receipt. Discard a disconnected/superseded result and drain
owned workers on close. A canceled write may have committed; authoritative
reload precedes another save. Source/query request generations reject late UI
results separately from revisions and render versions.

## Checked edits and public failures

The cell request has exactly `table_id`, `key`, `column_id`,
`expected_revision` and `value`. Revision is a canonical positive decimal
string through 9223372036854775807 and is mandatory. UI indices/A1 positions
never become write targets. Validate allowlisted column/scalar, load current
row, conceal denied current access before stale disclosure, compare expected
whole-row revision, construct/validate the complete proposed model and check
proposed policy inside one owned transaction. Only that field may change;
domain keys/read-only/hidden columns cannot be patched. Success increments
revision once and returns projected committed values. Failures roll back data
and journals; invalid/stale/denied outcomes retain client drafts. No automatic
retry, revision substitution or cross-cell merge occurs.

| HTTP | Error code | Meaning |
| --- | --- | --- |
| 400 | query / invalid_cursor | Malformed descriptor/URL parameters or incompatible continuation |
| 401 / 403 | unauthenticated / forbidden | Existing credential / operation policy refusal |
| 404 | not_found | Missing or concealed row |
| 409 | revision_conflict | Authorized row changed |
| 422 | validation / unsupported_type / read_only | Invalid scalar/model or refused edit capability |
| 503 | budget_exceeded / storage_unavailable | Candidate/VM work exhausted or unavailable storage |

Errors use the existing `{error:{code,message,details}}` envelope with fixed
value-free messages/issues, never query text, credentials, principal identity,
model values or SQL. OpenAPI must describe only actually implemented routes,
declared visible column types, bounded request/page shapes, credential security
and these public outcomes. No identity provider, package publication or
deployment is implied.

## Registration and edits

Register `app.tabular(resource, table_id="work_items", schema_version="1",
application_scope="my-application", columns=[TableColumn("title", "title",
"Title", sortable=True, searchable=True)])` after `App.include`. The optional
Backpack profile pins source `9a691aa551bf7fb42134168452bbdb2e1d454928`.
The registered Store must expose the selected public query columns. There is
no implicit schema inference or writable capability. Query
responses reauthorize candidates inside one owned transaction, retain stable
keys/revisions separately from projection and never expose hidden counts.
Disconnected/canceled work refuses its response; owned storage close drains any
already-dispatched operation. OpenAPI contains only the enabled routes.

Declare `TableColumn("title", "title", "Title", searchable=True,
sortable=True, writable=True)` to select editing. The included resource must
enable update and its store must expose `update_checked`. Identity fields,
model read-only metadata and write-only/undeclared fields cannot be writable.
When every column is read-only, the cell route remains absent.

Post the exact cell request above to `/_table/cell`. The result is
`{contract, table_id, schema_version, row: {key, revision, values}}`; `values`
contains all declared visible columns from the committed row. The expected
revision is mandatory, and number columns require actual finite floats;
integers and booleans are distinct. Rich objects/arrays return unsupported_type,
scalar/model mismatches return validation, and unavailable storage returns 503.
All errors omit submitted values and internal field mappings.

Current-row access is checked before revealing a stale revision. Both current
and complete proposed records use update policy in the same owned transaction.
Two independent writers editing different cells of one row can conflict; writes
to other rows remain independent. A cancellation or lost HTTP response can
follow commit: refresh authoritative pages and review the current row before
an explicit retry. Neither the transport nor storage substitutes a new revision
or automatically retries a write. Existing CRUD and single-record events keep
their contracts; this adds no formulas, multi-cell transactions or collection
subscriptions.
