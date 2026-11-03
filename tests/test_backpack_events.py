import asyncio
from dataclasses import dataclass, replace
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from expose_api import ConfigurationError, ConflictError, NotFoundError, Principal, Resource, ValidationError

try:
    from backpack_store import DispatchedCancellation, StorageError, TransactionError
    from expose_api.backpack import BackpackStore
    from expose_api.backpack_events import JournalModel
except ImportError:
    BackpackStore = None


@dataclass
class Item:
    id: str
    count: int = 0


async def open_store(path, *, events=4, event_namespace="item_events"):
    return await BackpackStore.open(Item, path, namespace="items", table="app_items", type_key="events.item",
                                    events=events, event_namespace=event_namespace)


WRITER = '''
import asyncio, sys
from dataclasses import dataclass, replace
from expose_api.backpack import BackpackStore
@dataclass
class Item:
    id: str
    count: int = 0
async def main():
    async with await BackpackStore.open(Item, sys.argv[1], namespace="items", table="app_items", type_key="events.item",
                                       events=4, event_namespace="item_events") as store:
        if len(sys.argv)>2:
            await store.create(sys.argv[2], Item(sys.argv[2]))
        else:
            for _ in range(3):
                await store.update("a", lambda v: replace(v, count=v.count+1))
asyncio.run(main())
'''


@unittest.skipIf(BackpackStore is None, "optional Backpack profile is not installed")
class PersistentEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.sqlite3"
        self.store = await open_store(self.path)
        self.addAsyncCleanup(self.store.aclose)
        await self.store.create("a", Item("a"))

    async def test_value_and_cursor_survive_restart_and_coalesce_updates(self):
        before = await self.store.event_snapshot("a")
        before.value.count = 99
        await self.store.update("a", lambda v: replace(v, count=1))
        await self.store.update("a", lambda v: replace(v, count=2))
        page = await self.store.event_changes(before.cursor, key="a")
        after = await self.store.event_snapshot("a")
        self.assertTrue(page.changed)
        self.assertFalse(page.resync)
        self.assertEqual(after.value.count, 2)
        await self.store.aclose()
        async with await open_store(self.path) as reopened:
            self.assertEqual(await reopened.event_snapshot("a"), after)
            self.assertFalse((await reopened.event_changes(after.cursor, key="a")).changed)
            self.assertTrue((await reopened.event_changes(before.cursor, key="a")).changed)

    async def test_validation_denial_transform_and_journal_failure_roll_back_together(self):
        before = await self.store.event_snapshot("a")
        resource = Resource(Item, store=self.store)
        protected = resource.for_principal(Principal("a"), lambda *args: False)
        for operation in (lambda: resource.update("a", {"count": True}), lambda: protected.delete("a"),
                          lambda: self.store.create("a", Item("a"))):
            with self.assertRaises((ValidationError, NotFoundError, ConflictError)):
                await operation()
        with patch.object(JournalModel, "_append", side_effect=RuntimeError("journal unavailable")):
            with self.assertRaises(TransactionError):
                await self.store.update("a", lambda v: replace(v, count=100))
            with self.assertRaises(TransactionError):
                await self.store.create("failed", Item("failed"))
            with self.assertRaises(TransactionError):
                await self.store.delete("a")
        self.assertEqual(await self.store.event_snapshot("a"), before)
        self.assertEqual((await self.store.get_row("a")).revision, "1")
        with self.assertRaises(NotFoundError):
            await self.store.get("failed")

    async def test_rotating_retention_bounds_rows_and_key_reservations(self):
        before = await self.store.event_snapshot("a")
        for i in range(20):
            await self.store.update("a", lambda v: replace(v, count=i))
        self.assertTrue((await self.store.event_changes(before.cursor, key="a")).resync)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_item_events_changes").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bp_application_keys WHERE namespace='item_events'").fetchone()[0], 5)
        current = await self.store.event_snapshot("a")
        await self.store.update("a", lambda v: replace(v, count=50))
        await self.store.update("a", lambda v: replace(v, count=51))
        self.assertTrue((await self.store.event_changes(current.cursor, key="a", limit=1)).resync)

    async def test_dispatched_cancellation_keeps_committed_change_receipt(self):
        entered, release = threading.Event(), threading.Event()
        before = await self.store.event_snapshot("a")
        def blocked(value):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return replace(value, count=1)
        task = asyncio.create_task(self.store.update("a", blocked))
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(DispatchedCancellation) as caught:
                await task
        finally:
            release.set()
        await caught.exception.receipt.wait()
        self.assertTrue((await self.store.event_changes(before.cursor, key="a")).changed)
        self.assertEqual((await self.store.event_snapshot("a")).value.count, 1)

    async def run_writer(self, *args):
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", WRITER, str(self.path), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        self.assertEqual(process.returncode, 0, error.decode())

    async def test_independent_processes_poll_only_committed_changes(self):
        before = await self.store.event_snapshot("a")
        await asyncio.gather(self.run_writer(), self.run_writer())
        after = await self.store.event_snapshot("a")
        self.assertEqual(after.value.count, 6)
        self.assertTrue((await self.store.event_changes(before.cursor, key="a")).resync)
        self.assertFalse((await self.store.event_changes(after.cursor, key="a")).changed)

    async def test_simultaneous_fresh_startup_owns_one_shared_stream(self):
        await self.store.aclose()
        self.path = Path(self.directory.name) / "fresh.sqlite3"
        await asyncio.gather(self.run_writer("first"), self.run_writer("second"))
        async with await open_store(self.path) as store:
            self.assertEqual([v.id for v in await store.list()], ["first", "second"])
            with closing(sqlite3.connect(self.path)) as connection:
                self.assertEqual(connection.execute("SELECT position FROM app_item_events_state").fetchone()[0], 2)

    async def test_explicit_enablement_preserves_resource_schema_and_refuses_mismatched_writers(self):
        await self.store.aclose()
        # Enabling creates a separately owned namespace; the resource registration stays compatible.
        async with await open_store(self.path, events=0, event_namespace=None) as legacy:
            self.assertEqual((await legacy.get("a")).count, 0)
            with self.assertRaises(ConfigurationError):
                await legacy.event_snapshot("a")
        with self.assertRaises(StorageError):
            await open_store(self.path, events=8)
        for kwargs in ({"events": True}, {"events": -1}, {"event_namespace": "items"}, {"event_namespace": None}):
            config = {"events": 4, "event_namespace": "item_events", **kwargs}
            with self.assertRaises(ConfigurationError):
                await open_store(self.path, **config)
