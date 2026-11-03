import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import unittest

from expose_api import App, ConfigurationError, MemoryStore, Principal
from websocket_client import Socket, register


@dataclass
class Note:
    id: str
    tenant: str = field(metadata={"expose_api": {"write_only": True}})
    title: str = "Initial"


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.identity = Principal("first", "a")
        self.app = App(authenticate=lambda req: self.identity, auth_challenge="Custom")
        self.store = MemoryStore(events=8)
        self.resource = self.app.include(Note, store=self.store, path="/notes", authorize=lambda req: True,
            record_policy=lambda p, op, value: p.tenant == value.tenant)
        await self.resource.create({"id": "a", "tenant": "a"})
        await self.resource.create({"id": "b", "tenant": "b"})
        register(self.app, self.resource, connections=2)

    async def socket(self, **kwargs):
        socket = Socket(self.app, **kwargs)
        self.addAsyncCleanup(socket.close)
        self.assertEqual((await socket.start())["type"], "websocket.accept")
        await socket.subscribe()
        return socket

    async def test_initial_and_committed_snapshots_preserve_field_visibility(self):
        socket = await self.socket()
        first = await socket.next()
        self.assertEqual(first["value"], {"id": "a", "title": "Initial"})
        await self.resource.update("a", {"title": "Changed"})
        second = await socket.next()
        self.assertEqual(second["value"]["title"], "Changed")
        self.assertNotEqual(first["cursor"], second["cursor"])
        self.assertNotIn("tenant", second["value"])

    async def test_missing_and_denied_admission_match_and_do_not_accept(self):
        for path in ("/notes/missing", "/notes/b", "/missing/a"):
            socket = Socket(self.app, path)
            self.addAsyncCleanup(socket.close)
            self.assertEqual(await socket.start(), {"type": "websocket.close", "code": 1008})
        self.assertEqual(self.app._sockets.tasks, {})

    async def test_origin_host_query_and_subprotocol_refusals(self):
        for changes in ({"headers": []}, {"query_string": b"token=private"}, {"subprotocols": []},
                        {"headers": [(b"origin", b"null"), (b"host", b"localhost")]},
                        {"headers": [(b"origin", b"http://localhost"), (b"host", b"other")]},
                        {"headers": [(b"origin", b"http://localhost")]*2+[(b"host", b"localhost")]}):
            socket = Socket(self.app)
            socket.scope.update(changes)
            self.addAsyncCleanup(socket.close)
            self.assertEqual((await socket.start())["code"], 1008)

    async def test_revocation_identity_transfer_and_deletion_close_generically(self):
        for change in ("revoke", "identity", "transfer", "delete"):
            socket = await self.socket()
            await socket.next()
            if change == "revoke":
                self.identity = None
            elif change == "identity":
                self.identity = Principal("second", "a")
            elif change == "transfer":
                await self.store.update("a", lambda value: Note("a", "b"))
            else:
                await self.store.delete("a")
            self.assertEqual(await socket.next(), {"type": "websocket.close", "code": 1008})
            await socket.close()
            self.identity = Principal("first", "a")
            if change == "transfer":
                await self.store.update("a", lambda value: Note("a", "a"))

    async def test_expired_resume_resyncs_then_sends_current_state(self):
        socket = Socket(self.app)
        self.addAsyncCleanup(socket.close)
        await socket.start()
        await socket.subscribe("0"*32)
        self.assertEqual(await socket.next(), {"v": 1, "type": "resync"})
        self.assertEqual((await socket.next())["value"]["title"], "Initial")

    async def test_capacity_and_disconnect_release_admission(self):
        first, second = await self.socket(), await self.socket()
        await first.next()
        await second.next()
        third = Socket(self.app)
        self.addAsyncCleanup(third.close)
        self.assertEqual((await third.start())["code"], 1013)
        await first.close()
        fourth = await self.socket()
        await fourth.next()
        await second.close()
        await fourth.close()
        self.assertEqual(self.app._sockets.tasks, {})

    async def test_invalid_binary_duplicate_extra_frames_and_oversize_are_closed(self):
        for message in ({"bytes": b"{}"}, {"text": "[]"}, {"text": '{"v":1,"v":1}'},
                        {"text": '{"v":true,"type":"subscribe","cursor":null}'},
                        {"text": "x"*(1024*1024+1)}):
            socket = Socket(self.app)
            self.addAsyncCleanup(socket.close)
            await socket.start()
            await socket.incoming.put({"type": "websocket.receive", **message})
            closed = await socket.next()
            self.assertIn(closed["code"], (1008, 1009))
            await socket.close()
        socket = await self.socket()
        await socket.next()
        await socket.subscribe()
        self.assertEqual((await socket.next())["code"], 1008)

    async def test_mounted_root_paths_and_send_failure_cleanup(self):
        socket = await self.socket(path="/prefix/notes/a", root_path="/prefix")
        await socket.next()
        await socket.close()
        async def failed(event):
            raise OSError("disconnected")
        socket = Socket(self.app, send=failed)
        self.addAsyncCleanup(socket.close)
        socket.task = asyncio.create_task(self.app(socket.scope, socket.incoming.get, socket.send))
        await socket.incoming.put({"type": "websocket.connect"})
        await asyncio.wait_for(socket.task, 2)
        self.assertEqual(self.app._sockets.tasks, {})

    async def test_stalled_send_times_out_and_cancellation_releases_tasks(self):
        async def stalled(event):
            await asyncio.Event().wait()
        socket = Socket(self.app, send=stalled)
        socket.task = asyncio.create_task(self.app(socket.scope, socket.incoming.get, socket.send))
        await socket.incoming.put({"type": "websocket.connect"})
        await asyncio.wait_for(socket.task, 1)
        self.assertEqual(self.app._sockets.tasks, {})
        socket = await self.socket()
        await socket.next()
        socket.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await socket.task
        self.assertEqual(self.app._sockets.tasks, {})

    async def test_auth_failures_redact_and_time_out_without_accepting(self):
        def failed(req):
            raise RuntimeError("private credential")
        self.app.authenticate = failed
        socket = Socket(self.app)
        self.addAsyncCleanup(socket.close)
        with self.assertLogs("expose_api", level="ERROR") as logs:
            self.assertEqual((await socket.start())["code"], 1011)
        self.assertNotIn("private", str(logs.output))
        async def stalled(req):
            await asyncio.Event().wait()
        self.app.authenticate = stalled
        socket = Socket(self.app)
        self.addAsyncCleanup(socket.close)
        self.assertEqual((await socket.start())["code"], 1008)

    async def test_lifespan_shutdown_closes_sockets_before_owned_store_cleanup(self):
        order = []
        @asynccontextmanager
        async def lifespan(app):
            yield
            order.append(len(app._sockets.tasks))
        self.app._lifespan_factory = lifespan
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        lifecycle = asyncio.create_task(self.app({"type": "lifespan"}, incoming.get, outgoing.put))
        await incoming.put({"type": "lifespan.startup"})
        self.assertEqual((await outgoing.get())["type"], "lifespan.startup.complete")
        socket = await self.socket()
        await socket.next()
        await incoming.put({"type": "lifespan.shutdown"})
        self.assertEqual((await socket.next())["code"], 1001)
        await asyncio.wait_for(lifecycle, 2)
        self.assertEqual(order, [0])

    def test_configuration_requires_protected_get_events_and_exact_bounds(self):
        for options in ({"origins": ("*",)}, {"hosts": ("*",)}, {"origins": ("http://localhost/",)},
                        {"connections": True}, {"poll_interval": float("nan")}, {"pending_limit": 0}):
            app = App(authenticate=lambda req: Principal("a"), auth_challenge="Custom")
            resource = app.include(Note, store=MemoryStore(events=2), authorize=lambda req: True, record_policy=lambda *args: True)
            config = {"origins": ("http://localhost",), "hosts": ("localhost",), **options}
            with self.assertRaises(ConfigurationError):
                app.subscribe(resource, **config)
        for kwargs in ({"public": True}, {"authorize": lambda req: True},
                       {"authorize": lambda req: True, "record_policy": lambda *args: True}):
            app = App(authenticate=lambda req: Principal("a"), auth_challenge="Custom")
            resource = app.include(Note, store=MemoryStore(), **kwargs)
            with self.assertRaises(ConfigurationError):
                register(app, resource)
