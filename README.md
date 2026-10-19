# expose-api

Turn ordinary typed dataclasses into JSON applications. Native ASGI, generated
CRUD, and a standard-library development runner, with **no runtime dependencies**.
Python 3.14+ is required.

```python
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from expose_api import App, MemoryStore


@dataclass
class Note:
    title: str
    content: str = ""
    id: UUID = field(default_factory=uuid4)


app = App(title="Notes API")
notes = app.include(Note, store=MemoryStore(), public=True)

if __name__ == "__main__":
    app.run()
```

Install a prepared wheel with `python -m pip install path/to/expose_api-0.1.0-py3-none-any.whl`.
From a checkout, `uv sync --locked` installs the package, and
`uv run python examples/notes.py` starts it at `http://127.0.0.1:8000`.

```sh
curl -i http://127.0.0.1:8000/notes \
  -H 'Content-Type: application/json' \
  -d '{"title":"First note"}'
curl http://127.0.0.1:8000/notes
```

The default path is the lowercase snake-case model name plus `s`. Supply
`path="/people"` for irregular names. Include each model with its own store before
the first ASGI call. Models and overlapping paths cannot be registered twice.

| Request | Result |
| --- | --- |
| GET /notes?offset=0&limit=100 | 200, JSON array |
| GET /notes/{id} | 200, record |
| POST /notes | 201, record and relative Location |
| PATCH /notes/{id} | 200, updated record |
| DELETE /notes/{id} | 204, empty body |

HEAD follows GET access checks and headers without a body. OPTIONS reports enabled
methods in Allow. Select routes with `operations=("list", "get")`; other CRUD
operations stay unavailable. Trailing slashes do not redirect. PUT is unsupported.
List results preserve insertion order, default to 100 records and allow at most
1,000. Pagination is not a snapshot across concurrent requests.

## Models and validation

Fields support strings, booleans, integers, finite floats, UUIDs, string/integer
enums, Literal, unions and nullable types, typed lists, string-keyed dictionaries,
and nonrecursive nested dataclasses. Defaults and factories behave as in ordinary
Python. Frozen and slots dataclasses work. Properties and methods are never
exposed. Unsupported annotations, unresolved references, InitVars, `init=False`
fields and incompatible constructor signatures fail at registration.

Validation is strict: booleans are not integers, strings are not numbers, unknown
fields fail, and null requires a nullable type. Integer JSON values may populate
float fields. The default identity field is `id`; select another with `id_field=`.
It must be a readable string, integer or UUID and cannot change through PATCH.
Strings must be nonempty single URL segments of at most 256 characters, without
control characters, slashes, backslashes, `.` or `..`. Required identities are
supplied at creation; default factories can generate them. Integer allocation is
not inferred. PATCH changes only supplied fields and replaces a nested field in
full, rather than recursively merging it.

Set visibility with ordinary dataclass metadata:

```python
created_by: str = field(default="system", metadata={"expose_api": {"read_only": True}})
password: str = field(default="", metadata={"expose_api": {"write_only": True}})
```

Read-only fields require defaults and reject input. Write-only fields accept input
and never appear in serialized output. Visibility applies to nested models too.
Defaults, constructor results and output values are validated; constructors and
`__post_init__` must remain fast and free of external side effects.

POST and PATCH require UTF-8 `application/json` or `application/*+json`. The app
counts actual streamed bytes against `max_body_size` (default 1 MiB), checks
Content-Length when supplied, rejects duplicate JSON keys and non-finite numbers,
and limits nesting to 32. Only offset/limit query parameters on list are supported.

Errors use a stable JSON shape:

```json
{"error":{"code":"validation","message":"Data does not match the model.","details":[{"path":["title"],"code":"type"}]}}
```

Validation errors return 422; malformed JSON/query/identity returns 400, denied
access 403, missing records/routes 404, unsupported methods 405, conflicts 409,
oversized bodies 413, unsupported media 415, and unexpected errors 500. Details
contain field paths and codes, without submitted values or dynamic dictionary
keys. Unexpected failures log their exception type without exception text.

## Access and direct Python use

Including a model requires either `public=True` or an authorization callback:

```python
def can_read(request):
    return request.operation in {"list", "get"}

app.include(Note, store=MemoryStore(), authorize=can_read)
```

The callback receives `Request(scope, model, operation, identity)`, may be async,
and must return a boolean. False denies before body parsing or store access.
`request.header(name)` returns a single header value, or None when absent or
repeated. OPTIONS exposes route metadata without invoking the policy.

Policies are resource-level access hooks. They do not authenticate credentials,
filter individual rows or provide tenant isolation. Those require an appropriate
application policy and store/service. Public exposure must be selected explicitly.

The returned `Resource` also supports direct Python calls:

```python
note = await notes.create({"title": "A note"})
note = await notes.update(note.id, {"content": "Changed"})
page = await notes.list(offset=0, limit=20)
await notes.delete(note.id)
```

Direct calls bypass HTTP authorization; the Python caller owns that trust boundary.
`ModelSchema` supplies independent create/patch/dump validation. `Store[T]` defines
async create/get/list/update/delete methods. An update applies a synchronous
transform inside the backend's atomic operation; if it fails, the stored record
must remain unchanged. `MemoryStore` copies values on input/output and keeps
mutations atomic, including validation before commit. It has no persistence,
cross-process state sharing or capacity eviction.

## ASGI and lifecycle

`app` is an ASGI 3 callable and can be used by independently installed ASGI
servers. HTTP and lifespan are implemented directly. Unsupported scope types,
including WebSocket, raise `ProtocolError`. Standard ASGI middleware can wrap the
application. Mounted root paths are reflected in relative Location headers;
generated links do not trust the Host header.

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    app.state["ready"] = True
    try:
        yield
    finally:
        app.state.clear()

app = App(lifespan=lifespan)
```

`app.run(host="127.0.0.1", port=8000)` blocks until interrupted. Inside an existing
event loop, use `async with serve(app, port=8000) as server:` and await
`server.serve_forever()`. Import `serve` from `expose_api`. Lifespan runs once and
pending connections close during shutdown.

The built-in runner is for development: HTTP/1.0/1.1, origin-form targets,
fixed-length request bodies, one request per connection, 32 KiB/100-header bounds,
10-second request timeout and at most 100 connections by default. `serve()` exposes
timeout/connection options. It rejects chunked requests, ambiguous lengths,
Expect, unsupported versions and invalid header syntax. It supplies no TLS,
HTTP/2, WebSocket upgrades, keep-alive or production server guarantees.

## Development and release preparation

```sh
uv sync --locked
uv run --frozen python -W error::ResourceWarning -m unittest discover -s tests -v
uv run --frozen python scripts/check_distribution.py
```

The distribution check verifies explicit archive allowlists, metadata, zero
runtime requirements, source/wheel parity and clean installations of both
artifacts, then runs the suite and example outside the checkout. CI runs on
Linux, macOS and Windows. [Release preparation](docs/releasing.md) describes the
separate publishing workflow.

The [Project](https://github.com/orgs/outside-labs/projects/14) and
[issues](https://github.com/outside-labs/expose_api/issues) track later SQLite,
OpenAPI, WebSocket and optional Hidden Moves/Backpack/CLI integrations. Version
0.1.0 does not include those features. License terms have not yet been specified.
