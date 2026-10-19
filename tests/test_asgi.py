import asyncio
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from expose_api import App, ConfigurationError, MemoryStore, ProtocolError
from asgi_client import request


@dataclass
class Note:
    title: str
    content: str = ""
    id: UUID = field(default_factory=uuid4)
    password: str = field(default="", metadata={"expose_api": {"write_only": True}})


class ASGITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = App()
        self.resource = self.app.include(Note, store=MemoryStore(), public=True)

    async def test_crud_and_status_headers(self):
        created = await request(self.app, method="POST", data={"title": "one", "password": "hidden"})
        self.assertEqual(created.status, 201)
        self.assertNotIn("password", created.json)
        location = created.headers[b"location"].decode()
        self.assertEqual(location, "/notes/" + created.json["id"])
        listing = await request(self.app)
        self.assertEqual(listing.json, [created.json])
        self.assertEqual((await request(self.app, location)).json, created.json)
        updated = await request(self.app, location, "PATCH", data={"content": "new"})
        self.assertEqual(updated.json["content"], "new")
        self.assertEqual((await request(self.app, location, "DELETE")).status, 204)
        self.assertEqual((await request(self.app, location)).status, 404)
        self.assertEqual((await request(self.app, location, "DELETE")).status, 404)

    async def test_head_options_and_method_negotiation(self):
        await self.resource.create({"title": "one"})
        get = await request(self.app)
        head = await request(self.app, method="HEAD")
        self.assertEqual((head.status, head.headers), (get.status, get.headers))
        self.assertEqual(head.body, b"")
        options = await request(self.app, method="OPTIONS")
        self.assertEqual(options.status, 204)
        self.assertEqual(options.headers[b"allow"], b"GET, HEAD, OPTIONS, POST")
        self.assertNotIn(b"content-length", options.headers)
        wrong = await request(self.app, method="PUT")
        self.assertEqual(wrong.status, 405)
        self.assertEqual(wrong.headers[b"allow"], options.headers[b"allow"])
        self.assertEqual((await request(self.app, "/missing")).status, 404)
        self.assertEqual((await request(self.app, "/notes/")).status, 404)

    async def test_operation_allowlist(self):
        app = App()
        app.include(Note, store=MemoryStore(), public=True, operations=["list"])
        self.assertEqual((await request(app, method="POST", data={})).status, 405)
        self.assertEqual((await request(app, "/notes/" + str(uuid4()))).status, 404)
        self.assertEqual((await request(app, method="OPTIONS")).headers[b"allow"], b"GET, HEAD, OPTIONS")

    async def test_explicit_access_and_registration_freeze(self):
        with self.assertRaises(ConfigurationError):
            App().include(Note, store=MemoryStore())
        for kwargs in ({"public": True, "authorize": lambda r: True}, {"public": 1}, {"operations": "get", "public": True}):
            with self.assertRaises(ConfigurationError):
                App().include(Note, store=MemoryStore(), **kwargs)
        for path in ("notes", "/notes/", "/{name}", "/a?b"):
            with self.assertRaises(ConfigurationError):
                App().include(Note, store=MemoryStore(), public=True, path=path)
        @dataclass
        class Other:
            id: str
        with self.assertRaises(ConfigurationError):
            self.app.include(Other, store=MemoryStore(), public=True, path="/notes/archive")
        await request(self.app)
        with self.assertRaises(ConfigurationError):
            self.app.include(Other, store=MemoryStore(), public=True)

    async def test_policy_denies_before_reading_or_storage(self):
        seen = []
        async def authorize(context):
            seen.append(context)
            return context.header("authorization") == "Bearer test-fixture"
        app = App()
        resource = app.include(Note, store=MemoryStore(), authorize=authorize)
        denied = await request(app, method="POST", body=b"invalid")
        self.assertEqual((denied.status, denied.reads), (403, 0))
        self.assertEqual(await resource.list(), [])
        allowed = await request(app, headers=[(b"authorization", b"Bearer test-fixture")])
        self.assertEqual(allowed.status, 200)
        self.assertEqual(seen[-1].operation, "list")
        self.assertIs(seen[-1].model, Note)
        denied_head = await request(app, method="HEAD")
        self.assertEqual((denied_head.status, denied_head.body), (403, b""))

    async def test_policy_failures_are_closed_and_redacted(self):
        for policy in (lambda r: "true", lambda r: 1):
            app = App()
            app.include(Note, store=MemoryStore(), authorize=policy)
            with self.assertLogs("expose_api", level="ERROR"):
                result = await request(app)
            self.assertEqual(result.status, 500)
        def broken(context):
            raise ValueError("credential-secret")
        app = App()
        app.include(Note, store=MemoryStore(), authorize=broken)
        with self.assertLogs("expose_api", level="ERROR") as logs:
            result = await request(app)
        self.assertNotIn("credential-secret", result.body.decode() + str(logs.output))

    async def test_validation_conflict_and_identifier_errors(self):
        invalid = await request(self.app, method="POST", data={"title": 4, "secret-value": "secret"})
        self.assertEqual(invalid.status, 422)
        self.assertNotIn(b"secret", invalid.body)
        identity = str(uuid4())
        await request(self.app, method="POST", data={"title": "one", "id": identity})
        self.assertEqual((await request(self.app, method="POST", data={"title": "two", "id": identity})).status, 409)
        self.assertEqual((await request(self.app, "/notes/bad")).status, 400)
        self.assertEqual((await request(self.app, "/notes/" + identity, "PATCH", data={"id": str(uuid4())})).status, 422)

    async def test_query_validation_and_pagination(self):
        for i in range(3):
            await self.resource.create({"title": str(i)})
        self.assertEqual([n["title"] for n in (await request(self.app, query=b"offset=1&limit=1")).json], ["1"])
        for query in (b"limit=0", b"limit=1001", b"limit=1&limit=2", b"x=1", b"offset=-1", b"limit=", b"x=%ZZ", b"x=%FF"):
            self.assertEqual((await request(self.app, query=query)).status, 400)

    async def test_media_json_and_chunk_limits(self):
        self.assertEqual((await request(self.app, method="POST", body=b"{}")).status, 415)
        for body in (b'{"title":"a","title":"b"}', b'{"title":NaN}', b'{"title":1e999}', b'\xff', b'{',
                     b'[' * 40 + b'0' + b']' * 40):
            with self.subTest(body=body):
                result = await request(self.app, method="POST", body=body, headers=[(b"content-type", b"application/json")])
                self.assertEqual(result.status, 400)
        scalar = await request(self.app, method="POST", body=b"[]", headers=[(b"content-type", b"application/json")])
        self.assertEqual(scalar.status, 422)
        valid = await request(self.app, method="POST", headers=[(b"content-type", b"application/test+json")],
                              events=[{"type": "http.request", "body": b'{"title":', "more_body": True},
                                      {"type": "http.request", "body": b'"split"}'}])
        self.assertEqual(valid.status, 201)
        app = App(max_body_size=10)
        app.include(Note, store=MemoryStore(), public=True)
        oversized = await request(app, method="POST", headers=[(b"content-type", b"application/json")],
                                  events=[{"type": "http.request", "body": b"123456", "more_body": True},
                                          {"type": "http.request", "body": b"123456"}])
        self.assertEqual(oversized.status, 413)

    async def test_content_length_is_not_trusted(self):
        headers = [(b"content-type", b"application/json")]
        for lengths, status in (([b"1"], 400), ([b"2", b"2"], 400), ([b"-1"], 400), ([b"9999999"], 413)):
            result = await request(self.app, method="POST", body=b"{}", headers=headers + [(b"content-length", n) for n in lengths])
            self.assertEqual(result.status, status)

    async def test_mount_and_percent_encoded_location(self):
        @dataclass
        class Record:
            id: str
        app = App()
        app.include(Record, store=MemoryStore(), path="/records", public=True)
        response = await request(app, "/api/records", "POST", root_path="/api", data={"id": "x ?#%é"})
        self.assertEqual(response.headers[b"location"], b"/api/records/x%20%3F%23%25%C3%A9")
        self.assertEqual((await request(app, "/api/records/x ?#%é", root_path="/api")).status, 200)

    async def test_disconnect_and_protocol_errors(self):
        disconnected = await request(self.app, method="POST", headers=[(b"content-type", b"application/json")],
                                     events=[{"type": "http.request", "body": b'{', "more_body": True},
                                             {"type": "http.disconnect"}])
        self.assertEqual(disconnected.messages, [])
        self.assertEqual(await self.resource.list(), [])
        with self.assertRaises(ProtocolError):
            await request(self.app, method="POST", headers=[(b"content-type", b"application/json")],
                          events=[{"type": "websocket.receive"}])
        with self.assertRaises(ProtocolError):
            await self.app({"type": "websocket"}, None, None)

    async def test_cancellation_propagates_and_send_errors_do_not_restart_response(self):
        entered = asyncio.Event()
        async def authorize(context):
            entered.set()
            await asyncio.Event().wait()
        app = App()
        app.include(Note, store=MemoryStore(), authorize=authorize)
        task = asyncio.create_task(request(app))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        sent = []
        async def send(event):
            sent.append(event)
            raise OSError("closed")
        with self.assertRaises(OSError):
            await self.app({"type": "http", "method": "GET", "path": "/notes"}, None, send)
        self.assertEqual(len(sent), 1)

    async def test_lifespan_success_and_failure(self):
        history = []
        @asynccontextmanager
        async def lifespan(app):
            history.append("start")
            app.state["ready"] = True
            try:
                yield
            finally:
                history.append("stop")
        async def run(app):
            events = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
            messages = []
            async def receive():
                return next(events)
            async def send(event):
                messages.append(event)
            await app({"type": "lifespan"}, receive, send)
            return messages
        messages = await run(App(lifespan=lifespan))
        self.assertEqual([m["type"] for m in messages], ["lifespan.startup.complete", "lifespan.shutdown.complete"])
        self.assertEqual(history, ["start", "stop"])
        @asynccontextmanager
        async def broken(app):
            raise ValueError("secret")
            yield
        with self.assertLogs("expose_api", level="ERROR"):
            messages = await run(App(lifespan=broken))
        self.assertEqual(messages, [{"type": "lifespan.startup.failed", "message": "Application startup failed."}])


if __name__ == "__main__":
    unittest.main()
