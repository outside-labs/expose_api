import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from expose_api import App, BearerAuth, ConfigurationError, Principal, TableColumn
from asgi_client import request

try:
    from backpack_store import TextSearch
    from expose_api.backpack import BackpackStore
except ImportError:
    BackpackStore = None


@dataclass
class Row:
    id: str
    title: str
    owner: str
    rank: int | None = None
    done: bool = False


@unittest.skipIf(BackpackStore is None, "qualified tabular Backpack source is not installed")
class TabularHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "table.sqlite"
        self.store = await self.open()
        self.addAsyncCleanup(self.store.aclose)
        for key, title, owner, rank in [("a", "Zulu", "bob", 2), ("b", "Alpha", "bob", 1),
                                         ("c", "Alpha", "alice", 1), ("d", "Alpha", "alice", 1),
                                         ("e", "café %_", "alice", None), ("f", "CAFÉ", "alice", 2)]:
            await self.store.create(key, Row(key, title, owner, rank))
        self.app = self.configure()

    async def open(self, **kwargs):
        return await BackpackStore.open(Row, self.path, namespace="rows", table="app_rows", type_key="test.rows", **kwargs)

    def configure(self, *, scan_limit=10000, max_work=1000000, policy=None, scope="test-app"):
        app = App(authenticate=BearerAuth(lambda token: Principal(token) if token in {"alice", "bob"} else None), auth_challenge="Bearer")
        resource = app.include(Row, store=self.store, path="/rows", authorize=lambda request: True,
                               record_policy=policy or (lambda principal, operation, row: row.owner == principal.subject))
        app.tabular(resource, table_id="work_items", schema_version="1", application_scope=scope, scan_limit=scan_limit, max_work=max_work,
                    columns=[TableColumn("title", "title", "Title", sortable=True, searchable=True),
                             TableColumn("rank", "rank", "Rank", sortable=True), TableColumn("done", "done", "Done", sortable=True)])
        return app

    async def query(self, data=None, *, principal="alice", app=None, **kwargs):
        return await request(app or self.app, "/rows/_table/query", "POST", data={"generation": 7, **(data or {})},
                             headers=[(b"authorization", f"Bearer {principal}".encode())], hold=True, **kwargs)

    async def test_visible_schema_sparse_denials_ties_and_independent_principals(self):
        schema = await request(self.app, "/rows/_table/schema", headers=[(b"authorization", b"Bearer alice")])
        self.assertEqual(schema.status, 200)
        self.assertNotIn("owner", schema.body.decode())
        self.assertTrue(all(not column["writable"] for column in schema.json["columns"]))
        first = await self.query({"limit": 1, "order": [{"column_id": "rank", "descending": False}]})
        self.assertEqual(first.status, 200, first.body)
        self.assertEqual([row["key"] for row in first.json["rows"]], ["c"])
        rest = await self.query({"cursor": first.json["next_cursor"], "limit": 10, "order": [{"column_id": "rank", "descending": False}]})
        self.assertEqual([row["key"] for row in rest.json["rows"]], ["d", "f", "e"])
        bob = await self.query(principal="bob")
        self.assertEqual([row["key"] for row in bob.json["rows"]], ["a", "b"])
        self.assertEqual(first.json["generation"], 7)
        self.assertEqual(first.json["rows"][0]["revision"], "1")
        self.assertEqual(first.headers[b"cache-control"], b"no-store")
        crud = await request(self.app, "/rows", headers=[(b"authorization", b"Bearer alice")])
        self.assertIsInstance(crud.json, list)

    async def test_literal_search_filters_hidden_refusal_and_strict_wire(self):
        found = await self.query({"search": {"fields": ["title"], "term": "%_"}, "filters": [{"column_id": "done", "operator": "eq", "value": False}]})
        self.assertEqual([row["key"] for row in found.json["rows"]], ["e"])
        for data in [{"search": {"fields": ["owner"], "term": "alice"}}, {"order": [{"column_id": "owner", "descending": False}]},
                     {"columns": ["owner"]}, {"columns": ["title", "title"]}, {"limit": True}, {"search": {"fields": ["rank"], "term": "1"}},
                     {"search": {"fields": ["title"], "term": "\ud800"}},
                     {"filters": [{"column_id": "rank", "operator": "eq", "value": True}]}, {"extra": "private"}]:
            response = await self.query(data)
            self.assertEqual(response.status, 400, response.body)
            self.assertNotIn(b"alice", response.body)
        for raw in (b"limit=1&limit=2", b"search=%ZZ"):
            self.assertEqual((await self.query(query=raw)).status, 400)
        duplicate = await request(self.app, "/rows/_table/query", "POST", body=b'{"generation":1,"generation":2}',
                                  headers=[(b"authorization", b"Bearer alice"), (b"content-type", b"application/json")], hold=True)
        self.assertEqual(duplicate.status, 400)
        unauthenticated = await self.query(principal="invalid")
        self.assertEqual((unauthenticated.status, unauthenticated.reads), (401, 0))
        self.assertEqual(unauthenticated.headers[b"cache-control"], b"no-store")

    async def test_cursor_scope_query_reopen_and_concurrent_change(self):
        first = await self.query({"limit": 1})
        cursor = first.json["next_cursor"]
        for data, principal, app in [({"cursor": cursor}, "bob", None),
                                      ({"cursor": cursor, "search": {"fields": ["title"], "term": "Alpha"}}, "alice", None),
                                      ({"cursor": cursor}, "alice", self.configure(scope="other-app"))]:
            response = await self.query(data, principal=principal, app=app)
            self.assertEqual((response.status, response.json["error"]["code"]), (400, "invalid_cursor"))
        await self.store.update_checked("d", expected_revision="1", transform=lambda row: replace(row, title="Changed"))
        next_page = await self.query({"cursor": cursor, "limit": 10})
        self.assertEqual(next_page.json["rows"][0]["revision"], "2")
        await self.store.aclose()
        self.store = await self.open()
        self.addAsyncCleanup(self.store.aclose)
        self.app = self.configure()
        reopened = await self.query({"cursor": cursor, "limit": 10})
        self.assertEqual(reopened.json["rows"], next_page.json["rows"])

    async def test_scan_and_sql_budgets_never_return_partial_pages(self):
        for app in (self.configure(scan_limit=2), self.configure(max_work=1)):
            result = await self.query({"limit": 2}, app=app)
            self.assertEqual((result.status, result.json["error"]["code"]), (503, "budget_exceeded"))
            self.assertNotIn(b"rows", result.body)
        self.assertEqual((await self.query()).status, 200)

    async def test_disconnect_cancel_and_worker_cleanup(self):
        disconnected = await request(self.app, "/rows/_table/query", "POST", data={"generation": 1},
            headers=[(b"authorization", b"Bearer alice")], events=[{"type": "http.request", "body": b'{"generation":1}'}, {"type": "http.disconnect"}])
        self.assertEqual(disconnected.status, 0)
        entered, release = threading.Event(), threading.Event()
        def policy(principal, operation, row):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return True
        task = asyncio.create_task(self.query(app=self.configure(policy=policy)))
        try:
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        self.assertEqual((await self.store.get_row("c")).revision, "1")
        await self.store.aclose()
        self.assertFalse(self.store._backend._thread.is_alive())

    async def test_openapi_configuration_and_no_default_table_routes(self):
        document = self.app.openapi()
        paths = document["paths"]
        self.assertIn("/rows/_table/schema", paths)
        self.assertIn("/rows/_table/query", paths)
        self.assertNotIn("/rows/_table/cell", paths)
        self.assertNotIn('"owner"', __import__('json').dumps(paths["/rows/_table/query"]))
        self.assertEqual(paths["/rows/_table/query"]["post"]["security"], [{"BearerAuth": []}])
        plain = App()
        resource = plain.include(Row, store=self.store, public=True, path="/rows")
        self.assertEqual((await request(plain, "/rows/_table/schema")).status, 404)
        with self.assertRaises(ConfigurationError):
            self.configure(max_work=True)
        new = App()
        resource = new.include(Row, store=self.store, public=True, path="/rows")
        with self.assertRaises(ConfigurationError):
            new.tabular(resource, table_id="t", schema_version="1", application_scope="a", columns=[TableColumn("x", "rank", "Rank", searchable=True)])
