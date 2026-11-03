from dataclasses import dataclass, field, replace
import importlib.util
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest

from expose_api import App, MemoryStore, ModelSchema, NotFoundError, Principal, Resource, RevisionConflictError, ValidationError
from asgi_client import request
from websocket_client import Socket, register
from test_enforce_type import IDENTITY, Record

AVAILABLE = importlib.util.find_spec("enforce_type") is not None
if AVAILABLE:
    from expose_api.enforce_type import EnforceTypeSchema
try:
    from expose_api.backpack import BackpackStore
except ImportError:
    BackpackStore = None


@dataclass
class OwnedRecord:
    id: str
    owner: str = field(metadata={"expose_api": {"write_only": True}})
    title: str = "Initial"
    done: bool = False
    server: int = field(default=1, metadata={"expose_api": {"read_only": True}})

    def __post_init__(self):
        if not self.title:
            raise ValueError("private constructor detail")


def protected(store, schema):
    app = App(authenticate=lambda req: Principal(req.header("x-subject") or "alice"), auth_challenge="Custom")
    resource = app.include(OwnedRecord, store=store, path="/notes", schema=schema, authorize=lambda req: True,
                           record_policy=lambda principal, operation, value: principal.subject == value.owner)
    return app, resource


@unittest.skipUnless(AVAILABLE, "optional enforce-type profile not installed")
class AdapterHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_http_transcript_and_openapi_match_builtin(self):
        transcripts, documents = [], []
        for kind in (ModelSchema, EnforceTypeSchema):
            app = App()
            app.include(Record, store=MemoryStore(), public=True, path="/notes", schema=kind(Record))
            documents.append(app.openapi())
            data = {"id": IDENTITY, "title": "Garden 🌱", "password": "private-value",
                    "child": {"text": "nested", "hidden": "private-value"}}
            calls = [("/notes", "POST", data), ("/notes", "GET", None),
                     ("/notes/" + IDENTITY, "GET", None),
                     ("/notes/" + IDENTITY, "PATCH", {"title": "Changed"}),
                     ("/notes/" + IDENTITY, "PATCH", {"entries": [{"private-key": True}]}),
                     ("/notes/" + IDENTITY, "PATCH", {"server": 2}),
                     ("/notes/" + IDENTITY, "PATCH", {"id": IDENTITY}),
                     ("/notes", "POST", {"id": IDENTITY, "title": "x", "private-key": "private-value"}),
                     ("/notes/" + IDENTITY, "DELETE", None), ("/notes/" + IDENTITY, "GET", None)]
            transcript = []
            for path, method, data in calls:
                response = await request(app, path, method, data=data)
                transcript.append((response.status, response.headers, response.body))
                self.assertNotIn("private", response.body.decode())
            self.assertEqual([status for status, _, _ in transcript], [201, 200, 200, 200, 422, 422, 422, 422, 204, 404])
            transcripts.append(transcript)
        self.assertEqual(transcripts[0], transcripts[1])
        self.assertEqual(documents[0], documents[1])
        response = documents[1]["components"]["schemas"]["Resource0Response"]
        self.assertNotIn("password", response["properties"])
        self.assertNotIn("hidden", response["properties"]["child"]["anyOf"][0]["properties"])
        documents[1]["paths"].clear()
        self.assertTrue(app.openapi()["paths"])

    async def test_protected_http_keeps_adapter_policy_and_atomic_failures(self):
        store = MemoryStore(events=8)
        schema = EnforceTypeSchema(OwnedRecord)
        app, resource = protected(store, schema)
        self.assertIs(resource.for_principal(Principal("alice"), lambda p, op, value: True).schema, schema)
        created = await request(app, method="POST", data={"id": "a", "owner": "alice"})
        self.assertEqual(created.status, 201)
        self.assertNotIn("owner", created.json)
        before = await store.event_snapshot("a")
        for data, status in (({"title": ""}, 422), ({"done": 1}, 422), ({"owner": "bob"}, 403)):
            response = await request(app, "/notes/a", "PATCH", data=data)
            self.assertEqual(response.status, status)
            self.assertEqual(response.headers[b"cache-control"], b"no-store")
            self.assertNotIn("private", response.body.decode())
            self.assertEqual(await store.event_snapshot("a"), before)
        denied = await request(app, "/notes/a", "PATCH", data={"title": "Denied"}, headers=[(b"x-subject", b"bob")])
        self.assertEqual(denied.status, 404)
        self.assertEqual(await store.event_snapshot("a"), before)
        self.assertEqual((await request(app, "/notes/a", "PATCH", data={"done": True})).json["done"], True)

    async def test_public_optional_example(self):
        example = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples" / "validated_notes.py"))
        response = await request(example["app"], method="POST", data={"id": "a", "title": "Example", "password": "private-value"})
        self.assertEqual(response.status, 201)
        self.assertEqual(response.json, {"id": "a", "title": "Example"})


@unittest.skipUnless(AVAILABLE and BackpackStore is not None, "optional validation/Backpack profile not installed")
class AdapterPersistentTests(unittest.IsolatedAsyncioTestCase):
    async def test_checked_rollback_event_projection_and_durable_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "notes.sqlite3"
            async def open_store():
                return await BackpackStore.open(OwnedRecord, path, namespace="notes", table="app_notes",
                    type_key="validation.note", events=8, event_namespace="note_events")
            schema = EnforceTypeSchema(OwnedRecord)
            async with await open_store() as store:
                app, resource = protected(store, schema)
                register(app, resource)
                await resource.create({"id": "a", "owner": "alice"})
                before = await store.event_snapshot("a")
                with self.assertRaises(ValidationError):
                    await resource.update("a", {"title": ""})
                def transform(current):
                    updated = replace(current, **schema.patch({"title": "Checked"}))
                    schema.dump(updated)
                    return updated
                with self.assertRaises(NotFoundError):
                    await store.update_checked("a", expected_revision="9", predicate=lambda value: False, transform=transform)
                with self.assertRaises(RevisionConflictError):
                    await store.update_checked("a", expected_revision="9", predicate=lambda value: True, transform=transform)
                self.assertEqual(await store.event_snapshot("a"), before)
                row = await store.update_checked("a", expected_revision="1", predicate=lambda value: value.owner == "alice", transform=transform)
                self.assertEqual((row.revision, row.value.title), ("2", "Checked"))
                socket = Socket(app)
                try:
                    self.assertEqual((await socket.start())["type"], "websocket.accept")
                    await socket.subscribe()
                    initial = await socket.next()
                    self.assertEqual(initial["value"], {"id": "a", "title": "Checked", "done": False, "server": 1})
                    await resource.for_principal(Principal("alice"), lambda p, op, value: value.owner == "alice").update("a", {"done": True})
                    snapshot = await socket.next()
                    self.assertTrue(snapshot["value"]["done"])
                    self.assertNotIn("owner", snapshot["value"])
                    self.assertNotEqual(initial["cursor"], snapshot["cursor"])
                finally:
                    await socket.close()
            async with await open_store() as reopened:
                app, resource = protected(reopened, schema)
                response = await request(app, "/notes/a")
                self.assertEqual(response.json, {"id": "a", "title": "Checked", "done": True, "server": 1})
                self.assertEqual((await reopened.get_row("a")).revision, "3")
                self.assertEqual((await reopened.event_snapshot("a")).cursor, snapshot["cursor"])
