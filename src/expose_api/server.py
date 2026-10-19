"""Bounded HTTP/1 development runner. TLS, chunking and upgrades are unsupported."""

import asyncio
import math
import re
from contextlib import asynccontextmanager
from http import HTTPStatus
from urllib.parse import unquote_to_bytes

from .asgi import ProtocolError, _error, logger
from .errors import ConfigurationError

_TOKEN = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_HEADER_LIMIT = 32768


class _Reject(Exception):
    def __init__(self, status):
        self.status = status


def _headers(raw):
    if len(raw) > _HEADER_LIMIT:
        raise _Reject(431)
    lines = raw[:-4].split(b"\r\n")
    if len(lines[0]) > 8192:
        raise _Reject(414)
    try:
        method, target, version = lines[0].split(b" ")
    except ValueError:
        raise _Reject(400) from None
    if not re.fullmatch(_TOKEN, method):
        raise _Reject(400)
    if version not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise _Reject(505)
    if not target.startswith(b"/") or target.startswith(b"//") or b"#" in target or any(c < 33 or c > 126 for c in target):
        raise _Reject(400)
    if re.search(rb"%(?![a-fA-F0-9]{2})", target):
        raise _Reject(400)
    raw_path, _, query = target.partition(b"?")
    try:
        path = unquote_to_bytes(raw_path).decode("utf-8")
    except UnicodeError:
        raise _Reject(400) from None
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise _Reject(400)
    if len(lines) > 101:
        raise _Reject(431)
    headers = []
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator or not re.fullmatch(_TOKEN, name) or any(c < 32 and c != 9 or c == 127 for c in value):
            raise _Reject(400)
        headers.append((name.lower(), value.strip(b" \t")))
    lengths = [v for k, v in headers if k == b"content-length"]
    hosts = [v for k, v in headers if k == b"host"]
    if len(hosts) > 1 or version == b"HTTP/1.1" and len(hosts) != 1:
        raise _Reject(400)
    if hosts and (not hosts[0] or re.search(rb"[\x00-\x20\x7f,/#?\\]", hosts[0])):
        raise _Reject(400)
    if any(k == b"transfer-encoding" for k, v in headers):
        raise _Reject(400)
    if any(k == b"expect" for k, v in headers):
        raise _Reject(417)
    if len(lengths) > 1 or lengths and not re.fullmatch(rb"[0-9]{1,20}", lengths[0]):
        raise _Reject(400)
    length = int(lengths[0]) if lengths else 0
    return method.decode("ascii"), version[5:].decode(), path, raw_path, query, headers, length


async def _connection(app, reader, writer, timeout):
    started, complete = False, False
    try:
        async with asyncio.timeout(timeout):
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
            except asyncio.LimitOverrunError:
                raise _Reject(431) from None
            except asyncio.IncompleteReadError:
                return
            method, version, path, raw_path, query, headers, remaining = _headers(raw)
            if remaining > app.max_body_size:
                raise _Reject(413)
            first = True
            expected, sent = None, 0

            async def receive():
                nonlocal remaining, first
                if remaining:
                    chunk = await reader.read(min(remaining, 65536))
                    if not chunk:
                        return {"type": "http.disconnect"}
                    remaining -= len(chunk)
                    first = False
                    return {"type": "http.request", "body": chunk, "more_body": remaining > 0}
                if first:
                    first = False
                    return {"type": "http.request", "body": b""}
                await reader.read(1)
                return {"type": "http.disconnect"}

            async def send(event):
                nonlocal started, complete, expected, sent
                if complete:
                    raise ProtocolError("Response is already complete.")
                if event["type"] == "http.response.start" and not started:
                    status = event["status"]
                    if type(status) is not int or not 200 <= status <= 599:
                        raise ProtocolError("Unsupported response status.")
                    response_headers = list(event.get("headers", ()))
                    for name, value in response_headers:
                        if type(name) is not bytes or type(value) is not bytes or not re.fullmatch(_TOKEN, name):
                            raise ProtocolError("Invalid response header.")
                        if any(c < 32 and c != 9 or c == 127 for c in value):
                            raise ProtocolError("Invalid response header value.")
                        if name.lower() in (b"transfer-encoding", b"connection"):
                            raise ProtocolError("Response framing belongs to the server.")
                    sizes = [v for k, v in response_headers if k.lower() == b"content-length"]
                    if len(sizes) > 1 or sizes and not re.fullmatch(rb"[0-9]{1,20}", sizes[0]):
                        raise ProtocolError("Invalid response length.")
                    if method != "HEAD" and status not in (204, 304):
                        expected = int(sizes[0]) if sizes else None
                    phrase = HTTPStatus(status).phrase if status in HTTPStatus else "Response"
                    header = f"HTTP/1.1 {status} {phrase}\r\n".encode()
                    header += b"".join(k + b": " + v + b"\r\n" for k, v in response_headers)
                    header += b"connection: close\r\n\r\n"
                    started = True
                    writer.write(header)
                elif event["type"] == "http.response.body" and started:
                    body = event.get("body", b"")
                    if type(body) is not bytes:
                        raise ProtocolError("Response body must be bytes.")
                    sent += len(body)
                    if expected is not None and sent > expected:
                        raise ProtocolError("Response exceeds its declared length.")
                    writer.write(body)
                    complete = not event.get("more_body", False)
                    if complete and expected is not None and sent != expected:
                        raise ProtocolError("Response does not match its declared length.")
                else:
                    raise ProtocolError("Invalid response event order.")
                await writer.drain()

            peer, local = writer.get_extra_info("peername"), writer.get_extra_info("sockname")
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.5"},
                     "http_version": version, "method": method, "scheme": "http", "path": path,
                     "raw_path": raw_path, "query_string": query, "root_path": "", "headers": headers,
                     "client": peer[:2] if peer else None, "server": local[:2] if local else None,
                     "state": dict(app.state)}
            await app(scope, receive, send)
    except (TimeoutError, _Reject) as exc:
        if not started:
            await _reject(writer, exc.status if isinstance(exc, _Reject) else 408)
    except (OSError, asyncio.IncompleteReadError):
        pass
    except Exception as exc:
        logger.error("Development connection failed (%s).", type(exc).__name__)
        if not started:
            await _reject(writer, 500)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _reject(writer, status):
    status, headers, body = _error(status, "http", HTTPStatus(status).phrase)
    header = f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n".encode()
    header += b"".join(k + b": " + v + b"\r\n" for k, v in headers)
    try:
        writer.write(header + b"connection: close\r\n\r\n" + body)
        await writer.drain()
    except OSError:
        pass


async def _phase(task, future, timeout, expected):
    done, _ = await asyncio.wait({task, future}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    if future in done and future.result() == expected:
        return
    if task.done() and not task.cancelled():
        task.exception()  # Retrieve failures without logging user exception messages.
    raise RuntimeError("Application lifespan did not complete successfully.")


@asynccontextmanager
async def serve(app, *, host="127.0.0.1", port=8000, request_timeout=10.0, max_connections=100):
    """Serve a bounded development HTTP profile; yield an asyncio.Server.

    Use a separate ASGI server for production transport features. Port 0 chooses
    an available port, useful for tests and embedding. Lifespan runs exactly once.
    """
    if type(port) is not int or not 0 <= port <= 65535:
        raise ConfigurationError("Port must be between 0 and 65535.")
    if type(request_timeout) not in (int, float) or not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ConfigurationError("request_timeout must be positive and finite.")
    if type(max_connections) is not int or max_connections < 1:
        raise ConfigurationError("max_connections must be positive.")
    loop = asyncio.get_running_loop()
    startup, shutdown = loop.create_future(), loop.create_future()
    events = asyncio.Queue()
    await events.put({"type": "lifespan.startup"})

    async def send(event):
        kind = event["type"]
        future = startup if kind.startswith("lifespan.startup.") else shutdown
        if future.done():
            raise ProtocolError("Duplicate lifespan response.")
        future.set_result(kind)

    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"},
                                    "state": app.state}, events.get, send))
    server, connections, ready, closing = None, set(), False, False

    def connected(reader, writer):
        if closing or len(connections) >= max_connections:
            writer.close()
            return
        connection = asyncio.create_task(_connection(app, reader, writer, request_timeout))
        connections.add(connection)
        connection.add_done_callback(connections.discard)

    try:
        await _phase(task, startup, request_timeout, "lifespan.startup.complete")
        ready = True
        server = await asyncio.start_server(connected, host, port, limit=_HEADER_LIMIT)
        yield server
    finally:
        closing = True
        if server is not None:
            server.close()
        pending = list(connections)
        for connection in pending:
            connection.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        try:
            if ready and not task.done():
                await events.put({"type": "lifespan.shutdown"})
                await _phase(task, shutdown, request_timeout, "lifespan.shutdown.complete")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def run(app, *, host="127.0.0.1", port=8000):
    """Block until interrupted; use serve() inside an existing event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Use 'async with serve(app)' inside an event loop.")

    async def main():
        async with serve(app, host=host, port=port) as server:
            await server.serve_forever()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
