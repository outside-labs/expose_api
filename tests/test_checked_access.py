from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from expose_api import NotFoundError, RevisionConflictError, ValidationError

try:
    from expose_api.backpack import BackpackStore
    from backpack_store import TransactionError
except ImportError:
    BackpackStore = None


@dataclass
class Note:
    id: str
    owner: str
    title: str = "Initial"


@unittest.skipIf(BackpackStore is None, "optional Backpack profile is not installed")
class CheckedAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = await BackpackStore.open(Note, Path(directory.name) / "notes.sqlite3", namespace="notes",
            table="app_notes", type_key="checked.note", events=4, event_namespace="note_events")
        self.addAsyncCleanup(self.store.aclose)
        await self.store.create("a", Note("a", "alice"))
        await self.store.update("a", lambda value: replace(value, title="Second"))

    async def test_predicate_precedes_stale_update_and_commits_only_authorized_values(self):
        before = await self.store.event_snapshot("a")
        calls = []
        def transform(value):
            calls.append(True)
            return replace(value, title="Saved")
        for key in ("a", "missing"):
            with self.assertRaises(NotFoundError):
                await self.store.update_checked(key, expected_revision="1", predicate=lambda value: value.owner == "bob", transform=transform)
        with self.assertRaises(RevisionConflictError):
            await self.store.update_checked("a", expected_revision="1", predicate=lambda value: True, transform=transform)
        self.assertEqual(calls, [])
        self.assertEqual((await self.store.event_snapshot("a")).cursor, before.cursor)
        row = await self.store.update_checked("a", expected_revision="2", predicate=lambda value: value.owner == "alice", transform=transform)
        self.assertEqual((row.revision, row.value.title), ("3", "Saved"))
        self.assertTrue((await self.store.event_changes(before.cursor, key="a")).changed)

    async def test_checked_delete_conceals_denial_and_preserves_stale_rows_and_journal(self):
        before = await self.store.event_snapshot("a")
        for key in ("a", "missing"):
            with self.assertRaises(NotFoundError):
                await self.store.delete_where(key, lambda value: False, expected_revision="1")
        with self.assertRaises(RevisionConflictError):
            await self.store.delete_where("a", lambda value: True, expected_revision="1")
        self.assertEqual((await self.store.event_snapshot("a")).cursor, before.cursor)
        await self.store.delete_where("a", lambda value: value.owner == "alice", expected_revision="2")
        with self.assertRaises(NotFoundError):
            await self.store.get("a")
        self.assertTrue((await self.store.event_changes(before.cursor, key="a")).changed)

    async def test_detached_predicate_failures_roll_back_and_reject_awaitables(self):
        before = await self.store.event_snapshot("a")
        def mutate(value):
            value.owner = "bob"
            return False
        def failed(value):
            raise RuntimeError("private predicate input")
        async def asynchronous(value):
            return True
        for operation in (
            lambda predicate: self.store.update_checked("a", expected_revision="2", predicate=predicate, transform=lambda value: value),
            lambda predicate: self.store.delete_where("a", predicate, expected_revision="2"),
        ):
            with self.assertRaises(NotFoundError):
                await operation(mutate)
            for predicate, error in ((failed, TransactionError), (lambda value: 1, ValidationError),
                                     (lambda value: asynchronous(value), ValidationError)):
                with self.assertRaises(error) as caught:
                    await operation(predicate)
                self.assertNotIn("private", str(caught.exception))
            with self.assertRaises(ValueError):
                await operation(asynchronous)
        after = await self.store.event_snapshot("a")
        self.assertEqual((after.cursor, after.value), (before.cursor, before.value))
