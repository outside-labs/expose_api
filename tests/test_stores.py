import asyncio
import unittest
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from expose_api import ConflictError, MemoryStore, NotFoundError, Resource, ValidationError


@dataclass(frozen=True, slots=True)
class Note:
    title: str
    content: str = ""
    tags: list[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.resource = Resource(Note, store=self.store)

    async def test_crud_on_frozen_slots_dataclass(self):
        note = await self.resource.create({"title": "one"})
        self.assertEqual(await self.resource.get(note.id), note)
        updated = await self.resource.update(note.id, {"content": "hello"})
        self.assertEqual(updated.content, "hello")
        self.assertEqual(updated.id, note.id)
        self.assertEqual(await self.resource.list(), [updated])
        self.assertIsNone(await self.resource.delete(note.id))
        for call in (self.resource.get(note.id), self.resource.update(note.id, {}), self.resource.delete(note.id)):
            with self.assertRaises(NotFoundError):
                await call

    async def test_copies_on_create_get_list_and_update(self):
        value = Note("first", tags=["a"])
        created = await self.store.create(str(value.id), value)
        value.tags.append("source")
        created.tags.append("result")
        fetched = await self.resource.get(value.id)
        self.assertEqual(fetched.tags, ["a"])
        fetched.tags.append("get")
        listing = await self.resource.list()
        listing[0].tags.append("list")
        patch = {"tags": ["patch"]}
        updated = await self.resource.update(value.id, patch)
        updated.tags.append("update")
        patch["tags"].append("input")
        self.assertEqual((await self.resource.get(value.id)).tags, ["patch"])

    async def test_order_pagination_and_reinsertion(self):
        notes = [await self.resource.create({"title": str(i)}) for i in range(4)]
        self.assertEqual(await self.resource.list(offset=1, limit=2), notes[1:3])
        self.assertEqual(await self.resource.list(offset=999), [])
        await self.resource.delete(notes[0].id)
        await self.resource.create(self.resource.schema.dump(notes[0]))
        self.assertEqual([n.id for n in await self.resource.list()], [n.id for n in notes[1:] + notes[:1]])
        for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 1001}, {"limit": False}):
            with self.assertRaises(ValidationError):
                await self.resource.list(**kwargs)

    async def test_duplicate_creates_and_disjoint_updates(self):
        identity = str(uuid4())
        results = await asyncio.gather(*[self.resource.create({"id": identity, "title": "one"})
                                         for _ in range(20)], return_exceptions=True)
        self.assertEqual(sum(isinstance(r, Note) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ConflictError) for r in results), 19)
        await asyncio.gather(self.resource.update(identity, {"title": "two"}),
                             self.resource.update(identity, {"content": "changed"}))
        note = await self.resource.get(identity)
        self.assertEqual((note.title, note.content), ("two", "changed"))

    async def test_failed_transforms_and_patches_leave_record_unchanged(self):
        note = await self.resource.create({"title": "one", "tags": ["original"]})
        def bad_transform(record):
            record.tags.append("leak")
            raise RuntimeError("failure")
        with self.assertRaises(RuntimeError):
            await self.store.update(str(note.id), bad_transform)
        for patch in ({"id": str(uuid4())}, {"title": False}, {"missing": "field"}):
            with self.assertRaises(ValidationError):
                await self.resource.update(note.id, patch)
        self.assertEqual(await self.resource.get(note.id), note)

    async def test_post_init_invariants_are_checked_before_commit(self):
        @dataclass
        class Record:
            id: str
            rename: bool = False
            def __post_init__(self):
                if self.rename:
                    self.id = "changed"
        resource = Resource(Record, store=MemoryStore())
        original = await resource.create({"id": "original"})
        with self.assertRaises(ValidationError):
            await resource.update("original", {"rename": True})
        self.assertEqual(await resource.get("original"), original)

    async def test_separate_stores_and_invalid_identifiers(self):
        other = Resource(Note, store=MemoryStore())
        note = await self.resource.create({"title": "one"})
        with self.assertRaises(NotFoundError):
            await other.get(note.id)
        for identity in (True, 1, "bad"):
            with self.assertRaises(ValidationError):
                await self.resource.get(identity)


if __name__ == "__main__":
    unittest.main()
