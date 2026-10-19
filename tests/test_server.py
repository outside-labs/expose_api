import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass

from expose_api import App, ConfigurationError, MemoryStore, serve


@dataclass
class Record:
    id: str
    value: int = 0


class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = App()
        self.resource = self.app.include(Record, store=MemoryStore(), public=True)

    async def exchange(self, server, raw):
        host, port = server.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(raw)
            await writer.drain()
            return await asyncio.wait_for(reader.read(), 3)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_real_http_create_get_patch_delete(self):
        async with serve(self.app, port=0) as server:
            body = b'{"id":"one","value":2}'
            response = await self.exchange(server, b"POST /records HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: "
                                            + str(len(body)).encode() + b"\r\n\r\n" + body)
            head, body = response.split(b"\r\n\r\n", 1)
            self.assertIn(b"201 Created", head)
            self.assertEqual(json.loads(body), {"id": "one", "value": 2})
            self.assertIn(b"location: /records/one", head)
            response = await self.exchange(server, b"GET /records/one HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertIn(b"200 OK", response)
            self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1])["value"], 2)
            head = await self.exchange(server, b"HEAD /records/one HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertEqual(head.split(b"\r\n\r\n", 1)[1], b"")
            body = b'{"value":3}'
            response = await self.exchange(server, b"PATCH /records/one HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: "
                                            + str(len(body)).encode() + b"\r\n\r\n" + body)
            self.assertIn(b'"value":3', response)
            response = await self.exchange(server, b"DELETE /records/one HTTP/1.0\r\n\r\n")
            self.assertIn(b"204 No Content", response)
            self.assertEqual(await self.resource.list(), [])

    async def test_invalid_framing_and_headers(self):
        cases = [
            (b"GET / HTTP/2.0\r\nHost: x\r\n\r\n", 505),
            (b"GET / HTTP/1.1\r\n\r\n", 400),
            (b"GET / HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n", 400),
            (b"GET / HTTP/1.1\r\nHost: x y\r\n\r\n", 400),
            (b"POST /records HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n", 400),
            (b"POST /records HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n", 400),
            (b"POST /records HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n", 400),
            (b"POST /records HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n\r\n", 417),
            (b"GET / HTTP/1.1\r\nHost: x\r\n folded: bad\r\n\r\n", 400),
            (b"GET http://example.org/ HTTP/1.1\r\nHost: x\r\n\r\n", 400),
            (b"GET //example.org/ HTTP/1.1\r\nHost: x\r\n\r\n", 400),
            (b"GET /bad%FF HTTP/1.1\r\nHost: x\r\n\r\n", 400),
            (b"GET /bad%0A HTTP/1.1\r\nHost: x\r\n\r\n", 400),
        ]
        async with serve(self.app, port=0) as server:
            for raw, status in cases:
                with self.subTest(raw=raw):
                    response = await self.exchange(server, raw)
                    self.assertTrue(response.startswith(f"HTTP/1.1 {status} ".encode()), response)

    async def test_timeout_size_and_header_limits(self):
        async with serve(self.app, port=0, request_timeout=0.1) as server:
            timeout = await self.exchange(server, b"GET /records HTTP/1.1\r\nHost: x\r\n")
            self.assertIn(b"408 Request Timeout", timeout)
            large = await self.exchange(server, b"POST /records HTTP/1.1\r\nHost: x\r\nContent-Length: 9999999\r\n\r\n")
            self.assertIn(b"413", large)
            headers = await self.exchange(server, b"GET / HTTP/1.1\r\nHost: x\r\nX-Big: " + b"a" * 34000 + b"\r\n\r\n")
            self.assertIn(b"431", headers)

    async def test_one_request_per_connection_and_unicode_identity(self):
        await self.resource.create({"id": "é%"})
        async with serve(self.app, port=0) as server:
            response = await self.exchange(server, b"GET /records/%C3%A9%25 HTTP/1.1\r\nHost: x\r\n\r\nGET / HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertEqual(response.count(b"HTTP/1.1"), 1)
            self.assertIn(b"200 OK", response)
            self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1])["id"], "é%")

    async def test_lifespan_once_and_pending_connections_close(self):
        history = []
        @asynccontextmanager
        async def lifespan(app):
            history.append("start")
            yield
            history.append("stop")
        app = App(lifespan=lifespan)
        app.include(Record, store=MemoryStore(), public=True)
        async with serve(app, port=0) as server:
            host, port = server.sockets[0].getsockname()[:2]
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"POST /records HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
            await writer.drain()
            await asyncio.sleep(0.01)
            self.assertEqual(history, ["start"])
        self.assertEqual(history, ["start", "stop"])
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        await writer.wait_closed()

    async def test_failed_startup_never_opens_listener(self):
        @asynccontextmanager
        async def lifespan(app):
            raise ValueError("private")
            yield
        with self.assertLogs("expose_api", level="ERROR"), self.assertRaises(RuntimeError):
            async with serve(App(lifespan=lifespan), port=0):
                self.fail("Started after failed lifespan")

    async def test_runner_configuration(self):
        for kwargs in ({"port": -1}, {"port": True}, {"request_timeout": 0}, {"request_timeout": float("inf")}, {"max_connections": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                async with serve(self.app, **kwargs):
                    self.fail("Invalid config accepted")
        with self.assertRaisesRegex(RuntimeError, "serve"):
            self.app.run()


if __name__ == "__main__":
    unittest.main()
