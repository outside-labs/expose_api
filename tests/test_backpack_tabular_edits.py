import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from expose_api import App, BearerAuth, ConfigurationError, Principal, TableColumn
from asgi_client import request

try:
    from expose_api.backpack import BackpackStore
except ImportError:
    BackpackStore = None


@dataclass
class Row:
    id: str
    title: str
    owner: str
    count: int | None = None
    done: bool = False
    score: float = 1.0
    stamp: str = field(default="retained", metadata={"expose_api": {"read_only": True}})

    def __post_init__(self):
        if self.title == "invalid":
            raise ValueError("private validation input")


@unittest.skipIf(BackpackStore is None, "qualified Backpack profile is not installed")
class TabularEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "edits.sqlite"
        self.store = await self.open()
        self.addAsyncCleanup(self.store.aclose)
        await self.store.create("a", Row("a", "Zulu", "alice"))
        await self.store.create("b", Row("b", "Alpha", "alice"))
        await self.store.create("c", Row("c", "Hidden", "bob"))
        self.app = self.configure()

    async def open(self):
        return await BackpackStore.open(Row, self.path, namespace="edits", table="app_edits", type_key="test.edit")

    def configure(self, *, store=None, policy=None, authorize=None):
        app = App(authenticate=BearerAuth(lambda token: Principal(token) if token == "alice" else None), auth_challenge="Bearer")
        resource = app.include(Row, store=store or self.store, path="/rows", authorize=authorize or (lambda request: True),
            record_policy=policy or (lambda principal, operation, row: row.owner == principal.subject and row.title != "denied"))
        app.tabular(resource, table_id="items", schema_version="1", application_scope="tests", columns=[
            TableColumn("label", "title", "Title", sortable=True, searchable=True, writable=True),
            TableColumn("count", "count", "Count", writable=True), TableColumn("done", "done", "Done", writable=True),
            TableColumn("score", "score", "Score", writable=True), TableColumn("stamp", "stamp", "Stamp")])
        return app

    async def edit(self, *, app=None, principal="alice", **changes):
        data = dict(table_id="items", key="b", column_id="label", expected_revision="1", value="Saved")
        data.update(changes)
        return await request(app or self.app, "/rows/_table/cell", "POST", data=data,
                             headers=[(b"authorization", f"Bearer {principal}".encode())], hold=True)

    async def test_sorted_filtered_target_and_committed_projection_reopen(self):
        page = await request(self.app, "/rows/_table/query", "POST", data={"generation": 1, "columns": ["label"],
            "order": [{"column_id": "label", "descending": False}], "search": {"fields": ["label"], "term": "Alpha"}},
            headers=[(b"authorization", b"Bearer alice")], hold=True)
        row = page.json["rows"][0]
        result = await self.edit(key=row["key"], expected_revision=row["revision"])
        self.assertEqual(result.status, 200, result.body)
        self.assertEqual(result.json["row"]["revision"], "2")
        self.assertEqual(result.json["row"]["values"]["label"], "Saved")
        self.assertNotIn(b"owner", result.body)
        self.assertEqual(result.headers[b"cache-control"], b"no-store")
        self.assertEqual((await self.store.get("a")).title, "Zulu")
        self.assertEqual((await self.store.get("b")).stamp, "retained")
        other = await self.open()
        self.addAsyncCleanup(other.aclose)
        self.assertEqual((await other.get_row("b")).revision, "2")
        self.assertEqual((await other.get("b")).owner, "alice")

    async def test_simultaneous_different_cells_conflict_and_other_rows_independent(self):
        other = await self.open()
        self.addAsyncCleanup(other.aclose)
        responses = await asyncio.gather(self.edit(), self.edit(app=self.configure(store=other), column_id="done", value=True))
        self.assertCountEqual([r.status for r in responses], [200, 409])
        self.assertEqual((await self.store.get_row("b")).revision, "2")
        self.assertEqual((await self.edit(key="a")).status, 200)
        stale = next(r for r in responses if r.status == 409)
        self.assertEqual(stale.json["error"]["code"], "revision_conflict")
        self.assertNotIn(b"Saved", stale.body)

    async def test_invalid_scalar_read_only_and_complete_model_preserve_revision(self):
        before = await self.store.get_row("b")
        for column, value, code in [("done", 1, "validation"), ("count", True, "validation"),
                ("count", 2**53, "validation"), ("score", 1, "validation"), ("label", None, "validation"),
                ("label", "\ud800", "validation"), ("label", "invalid", "validation"),
                ("label", {"decimal": "1.2"}, "unsupported_type"), ("stamp", "x", "read_only"),
                ("owner", "bob", "read_only"), ("id", "changed", "read_only")]:
            with self.subTest(column=column, value=value):
                response = await self.edit(column_id=column, value=value)
                self.assertEqual((response.status, response.json["error"]["code"]), (422, code), response.body)
                self.assertNotIn(b"private", response.body)
                self.assertNotIn(b"title", response.body)
                self.assertEqual(await self.store.get_row("b"), before)
        saved = await self.edit(column_id="count", value=4)
        self.assertEqual(saved.status, 200)
        cleared = await self.edit(column_id="count", value=None, expected_revision="2")
        self.assertIsNone(cleared.json["row"]["values"]["count"])

    async def test_current_concealed_before_stale_and_proposed_access_rollback(self):
        for key in ("c", "missing"):
            response = await self.edit(key=key, expected_revision="9")
            self.assertEqual((response.status, response.json["error"]["code"]), (404, "not_found"))
        before = await self.store.get_row("b")
        denied = await self.edit(value="denied")
        self.assertEqual(denied.status, 403)
        self.assertEqual(await self.store.get_row("b"), before)
        changed_policy = self.configure(policy=lambda *args: False)
        self.assertEqual((await self.edit(app=changed_policy, expected_revision="9")).status, 404)
        self.assertEqual((await self.edit(app=self.configure(authorize=lambda request: request.operation != "update"))).status, 403)
        unauthenticated = await self.edit(principal="invalid")
        self.assertEqual((unauthenticated.status, unauthenticated.reads), (401, 0))

    async def test_wire_admission_openapi_and_explicit_capabilities(self):
        for change in [dict(expected_revision="01"), dict(expected_revision="0"), dict(expected_revision=1),
                       dict(expected_revision=str(2**63)), dict(table_id="other"), dict(key="bad key"), dict(extra="private")]:
            self.assertEqual((await self.edit(**change)).status, 400)
        missing = await request(self.app, "/rows/_table/cell", "POST", data={"key": "b"},
                                 headers=[(b"authorization", b"Bearer alice")], hold=True)
        self.assertEqual(missing.status, 400)
        operation = self.app.openapi()["paths"]["/rows/_table/cell"]["post"]
        self.assertIn("409", operation["responses"])
        self.assertEqual(operation["security"], [{"BearerAuth": []}])
        self.assertIn("expected_revision", operation["requestBody"]["content"]["application/json"]["schema"]["required"])
        for column in [TableColumn("key", "id", "Key", writable=True), TableColumn("stamp", "stamp", "Stamp", writable=True)]:
            app = App()
            resource = app.include(Row, store=self.store, path="/rows", public=True)
            with self.assertRaises(ConfigurationError):
                app.tabular(resource, table_id="t", schema_version="1", application_scope="a", columns=[column])

    async def test_lost_and_canceled_committed_response_reconcile_fresh_read(self):
        profile = self.app._tables["/rows"]
        original = profile.edit
        committed = asyncio.Event()
        async def lost(data, principal):
            result = await original(data, principal)
            committed.set()
            await asyncio.Future()
            return result
        profile.edit = lost
        task = asyncio.create_task(self.edit())
        await asyncio.wait_for(committed.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        profile.edit = original
        row = await self.store.get_row("b")
        self.assertEqual((row.revision, row.value.title), ("2", "Saved"))
        self.assertEqual((await self.edit()).status, 409)
        self.assertEqual((await self.edit(value="Explicit retry", expected_revision=row.revision)).status, 200)
        await self.store.aclose()
        self.assertFalse(self.store._backend._thread.is_alive())
