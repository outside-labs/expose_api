# Optional compiled value validation

The adapter is an unreleased development profile. From this source checkout,
install `.[enforce-type]` or run `uv sync --locked --extra enforce-type` to
select published enforce-type 0.1.0. Both base packages retain zero runtime
requirements. Importing `expose_api` does not
import the optional validator; importing the adapter requires that extra.

```python
from dataclasses import dataclass
from expose_api import App, MemoryStore
from expose_api.enforce_type import EnforceTypeSchema

@dataclass
class Item:
    id: str
    title: str

app = App()
items = app.include(Item, store=MemoryStore(), public=True,
                    schema=EnforceTypeSchema(Item))
```

The selected schema must match the resource model and `id_field`. Protected
principal views retain it. Compiled scalar, UUID, enum and literal plans are
reused synchronously inside the existing API union/container/dataclass traversal.
API model admission, full constructor/default validation, partial patch behavior,
identity/URL rules, recursive visibility, depth and issue aggregation remain the
same. Compiled rejections pass through the API's existing leaf policy to retain
its error taxonomy and constructor timing. Native integer values in float output
are normalized using the public JSON load operation, as in the built-in schema.

The adapter deliberately selects the existing API annotation surface. It does
not select coercion, Annotated/metadata constraints, computed fields, wider native
model admission or new collection/error bounds. Dictionary key names and unknown
fields stay omitted from errors. Field constraints require their own API and
OpenAPI contract before adoption. Validation never performs asynchronous I/O;
store transforms and record policies retain their atomic ownership.

This is a finite compatibility adapter, with no performance claim or default
backend migration. The [assessment](https://github.com/wispsy/enforce_type/blob/main/docs/expose-api.md)
records the policy differences that prevent a whole-model replacement.

## Resource acceptance

`examples/validated_notes.py` is an executable public synthetic-resource example.
It keeps its password field hidden from responses. The selected adapter is also
qualified through full ASGI CRUD transcripts and detached OpenAPI documents
compared with the built-in schema. Nested secrets, read-only input, immutable
identities, unknown/dynamic keys and constructor failures retain the same
projection and error behavior.

Protected resource views preserve the schema and conceal denied records. Failed
validation and proposed-owner changes leave records and event cursors unchanged.
The separately selected Backpack profile verifies checked predicates/revisions,
rollback, current WebSocket projection and SQLite reopen using the same schema.
These checks do not widen the Backpack scalar model intersection or change
storage codec, authorization, journal ownership or subscription recovery rules.

## Installed profiles

Run `python scripts/check_distribution.py --enforce-type --backpack --receipt-file validation.json`
to qualify both candidate wheel and source artifacts. Base environments verify
the validator is absent. Each optional validation installation normally resolves
the declared extra, verifies the published engine's source digest and executes
ten model/HTTP cases without skips. The Backpack artifact profile also executes
all eleven cases, including persistent checked/event acceptance, alongside its
existing event suite. Linux, macOS and Windows retain these exact artifact,
module, dependency and test receipts in CI. No package upload is performed.
