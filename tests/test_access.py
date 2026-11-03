import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from expose_api import (
    AccessDeniedError, AccessScanError, AccessStore, ConfigurationError,
    MemoryStore, NotFoundError, Principal, Resource,
)

try:
    from expose_api.backpack import BackpackStore
except ImportError:
    BackpackStore = None


@dataclass
class Record:
    id: str
    tenant: str = field(metadata={"expose_api": {"write_only": True}})
    title: str = "Initial"


def policy(principal, operation, value):
    return value.tenant == principal.tenant


class AccessConformance:
    async def seed(self):
        self.resource = Resource(Record, store=self.store)
        for key, tenant in (("a", "other"), ("b", "mine"), ("c", "other"), ("d", "mine")):
            await self.resource.create({"id": key, "tenant": tenant})
        self.view = self.resource.for_principal(Principal("reader", "mine"), policy)

    async def test_concealed_get_list_before_pagination_and_scan_exhaustion(self):
        await self.seed()
        self.assertIsInstance(self.store, AccessStore)
        self.assertEqual([value.id for value in await self.view.list(offset=1, limit=1)], ["d"])
        self.assertEqual([value.id for value in await self.view.list()], ["b", "d"])
        self.assertEqual(await self.view.list(offset=5), [])
        for key in ("a", "missing"):
            with self.assertRaises(NotFoundError):
                await self.view.get(key)
        bounded = self.resource.for_principal(Principal("reader", "mine"), policy, scan_limit=2)
        with self.assertRaises(AccessScanError):
            await bounded.list(limit=2)
        self.assertEqual([value.id for value in await bounded.list(limit=1)], ["b"])
        exact = self.resource.for_principal(Principal("reader", "mine"), policy, scan_limit=4)
        self.assertEqual(len(await exact.list(limit=10)), 2)

    async def test_mutations_check_current_and_proposed_atomically(self):
        await self.seed()
        with self.assertRaises(AccessDeniedError):
            await self.view.create({"id": "e", "tenant": "other"})
        for action in (lambda: self.view.update("a", {"tenant": "mine"}), lambda: self.view.delete("a")):
            with self.assertRaises(NotFoundError):
                await action()
        before = await self.store.get_row("b") if BackpackStore and isinstance(self.store, BackpackStore) else None
        with self.assertRaises(AccessDeniedError):
            await self.view.update("b", {"tenant": "other"})
        if before:
            self.assertEqual((await self.store.get_row("b")).revision, before.revision)
        self.assertEqual((await self.view.get("b")).tenant, "mine")
        self.assertEqual((await self.view.update("b", {"title": "Changed"})).title, "Changed")
        await self.view.delete("b")
        with self.assertRaises(NotFoundError):
            await self.resource.get("b")
        self.assertEqual((await self.resource.get("a")).tenant, "other")

    async def test_policy_cannot_mutate_values_and_failures_leave_storage_unchanged(self):
        await self.seed()
        def mutating(principal, operation, value):
            value.title = "Policy mutation"
            return True
        view = self.resource.for_principal(Principal("trusted"), mutating)
        self.assertEqual((await view.get("a")).title, "Initial")
        self.assertEqual((await view.list())[0].title, "Initial")
        self.assertEqual((await view.update("a", {"title": "User update"})).title, "User update")
        def failed(principal, operation, value):
            value.title = "Uncommitted"
            raise RuntimeError("synthetic private failure")
        failed_view = self.resource.for_principal(Principal("trusted"), failed)
        for action in (lambda: failed_view.update("a", {"title": "Attempt"}), lambda: failed_view.delete("a")):
            with self.assertRaises(Exception):
                await action()
            self.assertEqual((await self.resource.get("a")).title, "User update")
        async def asynchronous(*args):
            return True
        with self.assertRaises(ConfigurationError):
            self.resource.for_principal(Principal("trusted"), asynchronous)
        for result in (1, None, "yes"):
            invalid = self.resource.for_principal(Principal("trusted"), lambda *args: result)
            with self.assertRaises(ConfigurationError):
                await invalid.get("a")
        wrapped = self.resource.for_principal(Principal("trusted"), lambda *args: asynchronous())
        with self.assertRaises(ConfigurationError):
            await wrapped.get("a")

    async def test_concurrent_views_keep_separate_principals(self):
        await self.seed()
        other = self.resource.for_principal(Principal("other", "other"), policy)
        mine, theirs = await asyncio.gather(self.view.list(), other.list())
        self.assertEqual([value.id for value in mine], ["b", "d"])
        self.assertEqual([value.id for value in theirs], ["a", "c"])


class MemoryAccessTests(AccessConformance, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()

    def test_configuration_refuses_unsupported_stores_and_mutable_identity(self):
        for kwargs in ({"subject": ""}, {"subject": "x", "scopes": {"read"}}, {"subject": "x", "tenant": "a\nb"}):
            with self.assertRaises(ConfigurationError):
                Principal(**kwargs)
        class LegacyStore:
            create = get = list = update = delete = lambda *args: None
        resource = Resource(Record, store=LegacyStore())
        with self.assertRaises(ConfigurationError):
            resource.for_principal(Principal("x"), policy)
        for limit in (0, True, 10001):
            with self.assertRaises(ConfigurationError):
                Resource(Record, store=self.store).for_principal(Principal("x"), policy, scan_limit=limit)


@unittest.skipIf(BackpackStore is None, "optional Backpack development profile is not installed")
class BackpackAccessTests(AccessConformance, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "access.sqlite3"
        self.store = await BackpackStore.open(Record, self.path, namespace="records", table="app_records", type_key="test.record")
        self.addAsyncCleanup(self.store.aclose)

    async def test_persisted_policy_and_guarded_delete_after_reopen(self):
        await self.seed()
        await self.store.aclose()
        store = await BackpackStore.open(Record, self.path, namespace="records", table="app_records", type_key="test.record")
        self.addAsyncCleanup(store.aclose)
        view = Resource(Record, store=store).for_principal(Principal("reader", "mine"), policy)
        self.assertEqual([row.id for row in await view.list()], ["b", "d"])
        with self.assertRaises(NotFoundError):
            await view.delete("a")
        await view.delete("b")
