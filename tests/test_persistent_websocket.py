import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from expose_api import App, ConflictError, Principal
from websocket_client import Socket, register

try:
    from expose_api.backpack import BackpackStore
except ImportError:
    BackpackStore = None


@dataclass
class Note:
    id: str
    tenant: str = field(metadata={"expose_api": {"write_only": True}})
    title: str = "Initial"


async def open_store(path):
    return await BackpackStore.open(Note, path, namespace="notes", table="app_notes", type_key="live.note",
                                    events=4, event_namespace="note_events")


WRITER = '''
import asyncio, sys
from dataclasses import dataclass, field, replace
from expose_api.backpack import BackpackStore
from backpack_store import TransactionError
@dataclass
class Note:
    id: str
    tenant: str = field(metadata={"expose_api": {"write_only": True}})
    title: str = "Initial"
async def main():
    async with await BackpackStore.open(Note, sys.argv[1], namespace="notes", table="app_notes", type_key="live.note",
                                       events=4, event_namespace="note_events") as store:
        if sys.argv[2] == "delete":
            await store.delete("a")
        elif sys.argv[2] == "fail":
            def bad(value):
                raise RuntimeError("private payload")
            try:
                await store.update("a", bad)
            except TransactionError:
                pass
            else:
                raise AssertionError("expected rollback")
        else:
            await store.update("a", lambda v: replace(v, title=sys.argv[2], tenant=sys.argv[3]))
asyncio.run(main())
'''


@unittest.skipIf(BackpackStore is None, "optional Backpack profile is not installed")
class PersistentSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "notes.sqlite3"
        self.store = await open_store(self.path)
        self.addAsyncCleanup(self.store.aclose)
        await self.store.create("a", Note("a", "a"))
        self.principals = {"first": Principal("first", "a"), "second": Principal("second", "a")}
        self.app, self.resource = self.build(self.store)

    def build(self, store, *, send_timeout=0.1):
        app = App(authenticate=lambda req: self.principals.get(req.header("x-subject") or "first"), auth_challenge="Custom")
        resource = app.include(Note, store=store, path="/notes", authorize=lambda req: True,
                               record_policy=lambda p, op, value: p.tenant == value.tenant)
        register(app, resource, pending_limit=2, send_timeout=send_timeout, auth_timeout=5.0)
        return app, resource

    async def socket(self, *, app=None, cursor=None, subject="first", send=None):
        socket = Socket(app or self.app, headers=[(b"origin", b"http://localhost"), (b"host", b"localhost"),
                                                  (b"x-subject", subject.encode())], send=send)
        self.addAsyncCleanup(socket.close)
        self.assertEqual((await socket.start())["type"], "websocket.accept")
        await socket.subscribe(cursor)
        return socket

    async def writer(self, operation, tenant="a"):
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", WRITER, str(self.path), operation, tenant,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        self.assertEqual(process.returncode, 0, error.decode())

    async def snapshot_named(self, socket, title):
        # A commit between snapshot and journal reads can repeat the baseline.
        # The contract recovers current state and permits duplicate snapshots.
        async with asyncio.timeout(3):
            while True:
                event = await socket.next()
                self.assertEqual(event["type"], "snapshot")
                self.assertEqual(set(event["value"]), {"id", "title"})
                if event["value"]["title"] == title:
                    return event

    async def closed_after(self, socket, baseline):
        # An already authorized snapshot can be in flight when access changes.
        async with asyncio.timeout(3):
            while True:
                event = await socket.next()
                if event["type"] == "websocket.close":
                    self.assertEqual(event["code"], 1008)
                    return
                self.assertEqual(event["type"], "snapshot")
                self.assertEqual(event["value"], baseline)

    async def test_other_process_delivers_to_independent_subscribers_after_commit(self):
        first, second = await self.socket(), await self.socket(subject="second")
        baseline, _ = await first.next(), await second.next()
        await self.writer("Committed")
        a, b = await self.snapshot_named(first, "Committed"), await self.snapshot_named(second, "Committed")
        self.assertEqual(a["value"], b["value"])
        self.assertEqual(a["value"], {"id": "a", "title": "Committed"})
        self.assertNotEqual(a["cursor"], baseline["cursor"])
        await first.close()
        await self.writer("Again")
        self.assertEqual((await self.snapshot_named(second, "Again"))["value"]["title"], "Again")

    async def test_worker_latency_uses_the_persistent_authentication_budget(self):
        original = self.store.event_snapshot
        async def delayed(key):
            await asyncio.sleep(0.15)
            return await original(key)
        with patch.object(self.store, "event_snapshot", delayed):
            socket = await self.socket()
            self.assertEqual((await socket.next())["value"], {"id": "a", "title": "Initial"})

    async def test_failed_external_transform_does_not_publish_or_advance_cursor(self):
        socket = await self.socket()
        before = await socket.next()
        await self.writer("fail")
        after = await self.store.event_snapshot("a")
        self.assertEqual((after.cursor, after.value.title), (before["cursor"], "Initial"))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(socket.outgoing.get(), 0.05)

    async def test_transfer_revokes_old_tenant_and_new_tenant_gets_only_current_projection(self):
        socket = await self.socket()
        baseline = await socket.next()
        await self.writer("New owner", "b")
        await self.closed_after(socket, baseline["value"])
        self.principals["second"] = Principal("second", "b")
        fresh = await self.socket(subject="second")
        self.assertEqual((await fresh.next())["value"], {"id": "a", "title": "New owner"})

    async def test_idle_credential_revocation_and_changed_principal_close_without_values(self):
        socket = await self.socket()
        await socket.next()
        del self.principals["first"]
        self.assertEqual((await socket.next())["code"], 1008)
        second = await self.socket(subject="second")
        await second.next()
        self.principals["second"] = Principal("changed", "a")
        self.assertEqual((await second.next())["code"], 1008)

    async def test_deletion_closes_and_permanent_storage_key_cannot_be_recreated(self):
        socket = await self.socket()
        baseline = await socket.next()
        await self.writer("delete")
        await self.closed_after(socket, baseline["value"])
        with self.assertRaises(ConflictError):
            await self.store.create("a", Note("a", "a", "Recreated"))

    async def test_retained_and_expired_cursor_recovery_across_store_and_app_restart(self):
        socket = await self.socket()
        initial = await socket.next()
        await socket.close()
        await self.writer("Offline change")
        await self.store.aclose()
        reopened = await open_store(self.path)
        self.addAsyncCleanup(reopened.aclose)
        app, resource = self.build(reopened)
        resumed = await self.socket(app=app, cursor=initial["cursor"])
        self.assertEqual((await resumed.next())["value"]["title"], "Offline change")
        await resumed.close()
        for i in range(5):
            await resource.update("a", {"title": str(i)})
        expired = await self.socket(app=app, cursor=initial["cursor"])
        self.assertEqual(await expired.next(), {"v": 1, "type": "resync"})
        self.assertEqual((await expired.next())["value"]["title"], "4")

    async def test_bounded_pending_changes_resync_after_a_held_snapshot(self):
        held, release = asyncio.Event(), asyncio.Event()
        # This acceptance deliberately holds send across three SQLite writes.
        # Keep it independent of the separate short send-timeout acceptance.
        app, _ = self.build(self.store, send_timeout=5.0)
        socket = Socket(app)
        first = True
        async def send(event):
            nonlocal first
            if event["type"] == "websocket.send" and first:
                first = False
                held.set()
                await release.wait()
            await socket.outgoing.put(event)
        socket.send = send
        self.addAsyncCleanup(socket.close)
        await socket.start()
        await socket.subscribe()
        try:
            await asyncio.wait_for(held.wait(), 1)
            for i in range(3):
                await self.resource.update("a", {"title": str(i)})
        finally:
            release.set()
        self.assertEqual((await socket.next())["type"], "snapshot")
        self.assertEqual(await socket.next(), {"v": 1, "type": "resync"})
        self.assertEqual((await socket.next())["value"]["title"], "2")

    async def test_uncommitted_worker_mutation_never_delivers_a_speculative_value(self):
        socket = await self.socket()
        initial = await socket.next()
        entered, release = threading.Event(), threading.Event()
        def blocked(value):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return replace(value, title="Committed after release")
        mutation = asyncio.create_task(self.store.update("a", blocked))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            self.assertTrue(socket.outgoing.empty())
        finally:
            release.set()
        await mutation
        update = await self.snapshot_named(socket, "Committed after release")
        self.assertEqual(update["value"]["title"], "Committed after release")
        self.assertNotEqual(update["cursor"], initial["cursor"])
