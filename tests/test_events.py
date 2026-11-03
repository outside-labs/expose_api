import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import unittest

from expose_api import ConfigurationError, EventStore, MemoryStore, NotFoundError


@dataclass
class Item:
    id: str
    count: int = 0


class MemoryEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_atomic_snapshots_coalesce_committed_changes(self):
        store = MemoryStore(events=8)
        self.assertIsInstance(store, EventStore)
        await store.create("a", Item("a"))
        before = await store.event_snapshot("a")
        before.value.count = 99
        await store.update("a", lambda v: replace(v, count=1))
        await store.update("a", lambda v: replace(v, count=2))
        page = await store.event_changes(before.cursor, key="a")
        after = await store.event_snapshot("a")
        self.assertTrue(page.changed)
        self.assertFalse(page.resync)
        self.assertEqual((page.cursor, after.value.count), (after.cursor, 2))
        self.assertFalse((await store.event_changes(after.cursor, key="a")).changed)
        self.assertEqual(len(after.cursor), 32)

    async def test_failed_transforms_conflicts_and_denied_deletes_emit_nothing(self):
        store = MemoryStore(events=8)
        await store.create("a", Item("a"))
        before = await store.event_snapshot("a")
        def bad(value):
            value.count = 100
            raise ValueError("private")
        with self.assertRaises(ValueError):
            await store.update("a", bad)
        with self.assertRaises(ValueError):
            await store.create("a", Item("a"))
        with self.assertRaises(NotFoundError):
            await store.delete_where("a", lambda v: False)
        self.assertEqual(await store.event_snapshot("a"), before)

    async def test_other_keys_advance_cursor_without_notifying_and_delete_recreate_is_visible(self):
        store = MemoryStore(events=8)
        await store.create("a", Item("a"))
        before = await store.event_snapshot("a")
        await store.create("b", Item("b"))
        page = await store.event_changes(before.cursor, key="a")
        self.assertFalse(page.changed)
        self.assertNotEqual(page.cursor, before.cursor)
        await store.delete("a")
        with self.assertRaises(NotFoundError):
            await store.event_snapshot("a")
        await store.create("a", Item("a", 5))
        self.assertTrue((await store.event_changes(page.cursor, key="a")).changed)

    async def test_retention_overflow_backlog_and_unknown_cursor_require_fresh_state(self):
        store = MemoryStore(events=2)
        await store.create("a", Item("a"))
        before = await store.event_snapshot("a")
        for i in range(2):
            await store.update("a", lambda v: replace(v, count=i))
        self.assertFalse((await store.event_changes(before.cursor, key="a", limit=2)).resync)
        self.assertTrue((await store.event_changes(before.cursor, key="a", limit=1)).resync)
        await store.update("a", lambda v: replace(v, count=3))
        self.assertTrue((await store.event_changes(before.cursor, key="a")).resync)
        self.assertTrue((await store.event_changes("0"*32, key="a")).resync)
        self.assertEqual(len(store._changes), 2)

    async def test_disabled_and_invalid_configuration_fail_before_mutation(self):
        for value in (-1, True, 4097, None):
            with self.assertRaises(ConfigurationError):
                MemoryStore(events=value)
        store = MemoryStore()
        await store.create("a", Item("a"))
        with self.assertRaises(ConfigurationError):
            await store.event_snapshot("a")
        enabled = MemoryStore(events=1)
        for key in ("", "x"*257, "x\n", 1):
            with self.assertRaises(ValueError):
                await enabled.create(key, Item("a"))
        self.assertEqual(await enabled.list(), [])
        for cursor, limit in (("bad", 1), ("0"*32, True), ("0"*32, 1025)):
            with self.assertRaises(ValueError):
                await enabled.event_changes(cursor, key="a", limit=limit)

    async def test_real_threads_never_pair_a_new_value_with_an_old_cursor(self):
        store = MemoryStore(events=256)
        await store.create("a", Item("a"))
        before = await store.event_snapshot("a")
        def increment():
            asyncio.run(store.update("a", lambda v: replace(v, count=v.count+1)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            await asyncio.gather(*(asyncio.wrap_future(pool.submit(increment)) for _ in range(100)))
        after = await store.event_snapshot("a")
        self.assertEqual(after.value.count, 100)
        self.assertFalse((await store.event_changes(after.cursor, key="a")).changed)
        self.assertTrue((await store.event_changes(before.cursor, key="a", limit=100)).changed)
