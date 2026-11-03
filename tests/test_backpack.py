import asyncio
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from expose_api import App, ConflictError, NotFoundError, Resource, ValidationError
from expose_api.errors import RevisionConflictError
from asgi_client import request

try:
    from expose_api.backpack import BackpackStore
    from backpack_store import DispatchedCancellation, TransactionError
except ImportError:
    BackpackStore = None


@dataclass
class Item:
    id: str
    title: str
    count: int = 0


async def open_store(path):
    return await BackpackStore.open(Item, path, namespace="items", table="app_items", type_key="example.item")


@unittest.skipIf(BackpackStore is None, "optional Backpack development profile is not installed")
class BackpackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "items.sqlite3"
        self.store = await open_store(self.path)
        self.addAsyncCleanup(self.store.aclose)
        self.resource = Resource(Item, store=self.store)

    async def test_detached_values_crud_and_restart(self):
        value = Item("a", "First")
        created = await self.store.create("a", value)
        value.title = created.title = "caller mutation"
        fetched = await self.resource.get("a")
        self.assertEqual(fetched.title, "First")
        fetched.title = "detached"
        with self.assertRaises(ConflictError):
            await self.store.create("a", Item("a", "Duplicate"))
        await self.resource.create({"id": "c", "title": "Third"})
        await self.resource.create({"id": "b", "title": "Second"})
        self.assertEqual([row.id for row in await self.store.list(offset=1, limit=1)], ["b"])
        await self.store.aclose()
        reopened = await open_store(self.path)
        self.addAsyncCleanup(reopened.aclose)
        self.assertEqual((await reopened.get("a")).title, "First")
        await reopened.delete("a")
        with self.assertRaises(NotFoundError):
            await reopened.get("a")
        with self.assertRaises(ConflictError):
            await reopened.create("a", Item("a", "Tombstone"))

    async def test_atomic_transforms_invalid_outputs_and_stale_revision(self):
        await self.resource.create({"id": "a", "title": "First"})
        original = await self.store.get_row("a")
        await asyncio.gather(*[self.store.update("a", lambda value: replace(value, count=value.count + 1)) for _ in range(12)])
        self.assertEqual((await self.store.get("a")).count, 12)
        with self.assertRaises(RevisionConflictError):
            await self.store.update_checked("a", expected_revision=original.revision, transform=lambda value: value)
        with self.assertRaises(RevisionConflictError):
            await self.store.delete_checked("a", expected_revision=original.revision)
        with self.assertRaises(ValidationError):
            await self.store.delete_checked("a", expected_revision="01")
        for transform in (lambda value: replace(value, id="changed"), lambda value: replace(value, count="private")):
            with self.assertRaises(ValidationError):
                await self.store.update("a", transform)
        def failed(value):
            value.title = "uncommitted"
            raise RuntimeError("private value")
        with self.assertRaises(TransactionError) as caught:
            await self.store.update("a", failed)
        self.assertNotIn("private value", str(caught.exception))
        self.assertEqual((await self.store.get_row("a")).revision, "13")
        self.assertEqual((await self.store.get("a")).title, "First")

    async def test_transform_validation_is_http_422_after_rollback(self):
        @dataclass
        class Bounded:
            id: str
            title: str
            def __post_init__(self):
                if len(self.title) > 10:
                    raise ValueError("private submitted title")
        store = await BackpackStore.open(Bounded, self.path, namespace="bounded", table="app_bounded", type_key="example.bounded")
        self.addAsyncCleanup(store.aclose)
        app = App()
        app.include(Bounded, store=store, path="/bounded", public=True)
        await request(app, "/bounded", "POST", data={"id": "a", "title": "Initial"})
        before = await store.get_row("a")
        invalid = await request(app, "/bounded/a", "PATCH", data={"title": "private submitted title"})
        self.assertEqual(invalid.status, 422)
        self.assertEqual(invalid.json["error"]["details"], [{"path": [], "code": "constructor"}])
        self.assertNotIn("private", invalid.body.decode())
        after = await store.get_row("a")
        self.assertEqual((after.value.title, after.revision), (before.value.title, before.revision))

    async def test_http_resource_uses_validated_persistent_store(self):
        app = App()
        app.include(Item, store=self.store, path="/items", public=True)
        created = await request(app, "/items", "POST", data={"id": "a", "title": "First"})
        self.assertEqual(created.status, 201)
        invalid = await request(app, "/items/a", "PATCH", data={"count": True})
        self.assertEqual(invalid.status, 422)
        self.assertEqual((await request(app, "/items/a")).json["count"], 0)
        await self.resource.update("a", {"count": 1})
        async def stale_update(key, transform):
            return (await self.store.update_checked(key, transform=transform, expected_revision="1")).value
        self.store.update = stale_update
        stale = await request(app, "/items/a", "PATCH", data={"count": 2})
        self.assertEqual((stale.status, stale.json["error"]["code"]), (409, "revision_conflict"))
        self.assertEqual((await request(app, "/items/a")).json["count"], 1)
        self.assertEqual((await request(app, "/items/a", "DELETE")).status, 204)
        self.assertEqual((await request(app, "/items/a")).status, 404)

    async def test_pagination_bounds(self):
        for kwargs in ({"offset": True}, {"offset": 10001}, {"offset": -1}, {"limit": False}, {"limit": 1001}):
            with self.assertRaises(ValueError):
                await self.store.list(**kwargs)

    async def test_dispatched_cancellation_retains_outcome_and_close_drains(self):
        await self.store.create("a", Item("a", "First"))
        entered, release = threading.Event(), threading.Event()
        def blocked(value):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return replace(value, count=1)
        task = asyncio.create_task(self.store.update("a", blocked))
        try:
            async def wait_entered():
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(wait_entered(), 5)
            task.cancel()
            with self.assertRaises(DispatchedCancellation) as caught:
                await task
        finally:
            release.set()
        row = await caught.exception.receipt.wait()
        self.assertEqual(row.value.count, 1)
        await self.store.aclose()
        reopened = await open_store(self.path)
        self.addAsyncCleanup(reopened.aclose)
        self.assertEqual((await reopened.get("a")).count, 1)

    async def test_independent_processes_do_not_lose_atomic_updates(self):
        await self.store.create("a", Item("a", "First"))
        code = '''
import asyncio, sys
from dataclasses import dataclass, replace
from expose_api.backpack import BackpackStore
@dataclass
class Item:
    id: str
    title: str
    count: int = 0
async def main():
    store = await BackpackStore.open(Item, sys.argv[1], namespace="items", table="app_items", type_key="example.item")
    try:
        for _ in range(10):
            await store.update("a", lambda value: replace(value, count=value.count + 1))
    finally:
        await store.aclose()
asyncio.run(main())
'''
        processes = [await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code, str(self.path),
                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for _ in range(2)]
        for process in processes:
            output = await asyncio.wait_for(process.communicate(), 20)
            self.assertEqual(process.returncode, 0, output[1].decode())
        self.assertEqual((await self.store.get("a")).count, 20)
