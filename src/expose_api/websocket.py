"""Owned, bounded ASGI single-record subscriptions."""

import asyncio
import inspect
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .access import Principal, RecordAccess
from .errors import ConfigurationError, NotFoundError
from .events import EventStore, _cursor

SUBPROTOCOL = "expose-api.events.v1"
logger = logging.getLogger("expose_api")


class _Close(Exception):
    def __init__(self, code):
        self.code = code


def _host(value):
    return (type(value) is str and len(value) <= 256
            and re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?", value) is not None)


@dataclass(frozen=True, slots=True)
class Subscription:
    origins: tuple[str, ...]
    hosts: tuple[str, ...]
    connections: int = 64
    pending_limit: int = 64
    max_message_bytes: int = 1024 * 1024
    poll_interval: float = 0.25
    send_timeout: float = 5.0
    auth_timeout: float = 5.0
    subscribe_timeout: float = 5.0
    lifetime: float = 3600.0

    def __post_init__(self):
        for values in (self.origins, self.hosts):
            if type(values) is not tuple or not 1 <= len(values) <= 64:
                raise ConfigurationError("Subscriptions require finite exact origin/host tuples.")
        if not all(_host(value) for value in self.hosts):
            raise ConfigurationError("Subscriptions require exact host names and ports.")
        for origin in self.origins:
            if type(origin) is not str or len(origin) > 512:
                raise ConfigurationError("Invalid subscription origin.")
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not _host(parsed.netloc) or origin != f"{parsed.scheme}://{parsed.netloc}":
                raise ConfigurationError("Origins must be exact HTTP(S) origins without paths.")
        for name, lower, upper in (("connections", 1, 1024), ("pending_limit", 1, 1024),
                                    ("max_message_bytes", 128, 1024 * 1024)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ConfigurationError("Invalid subscription integer bound.")
        for name in ("poll_interval", "send_timeout", "auth_timeout", "subscribe_timeout", "lifetime"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0.01 <= value <= (3600 if name == "lifetime" else 30):
                raise ConfigurationError("Invalid subscription time bound.")


class EventSockets:
    def __init__(self, app):
        self.app = app
        self.mounts = {}
        self.tasks = {}
        self.stopping = False
        self.owner = None

    def register(self, mount, options):
        if (mount.path in self.mounts or "get" not in mount.operations or mount.record_policy is None
                or not isinstance(mount.resource.store, EventStore) or mount.resource.store.events_enabled is not True):
            raise ConfigurationError("Subscriptions require one protected get-enabled resource with events enabled.")
        self.mounts[mount.path] = (mount, Subscription(**options))

    async def stop(self):
        self.stopping = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __call__(self, scope, receive, send):
        from .asgi import Request, _constant, _pairs

        scope = dict(scope)
        scope["headers"] = tuple(scope.get("headers", ()))
        path, root = scope["path"], scope.get("root_path", "").rstrip("/")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):] or "/"
        selected = next(((m, c, path[len(p)+1:]) for p, (m, c) in self.mounts.items()
                         if path.startswith(p + "/") and "/" not in path[len(p)+1:]), None)
        config = selected[1] if selected else Subscription(("http://localhost",), ("localhost",))
        task = asyncio.current_task()
        owned = []

        async def emit(event):
            try:
                async with asyncio.timeout(config.send_timeout):
                    await send(event)
            except TimeoutError:
                raise _Close(1013) from None

        async def output(data):
            text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(text.encode("utf-8")) > config.max_message_bytes:
                raise _Close(1009)
            await emit({"type": "websocket.send", "text": text})

        try:
            async with asyncio.timeout(config.subscribe_timeout):
                connect = await receive()
            if connect.get("type") == "websocket.disconnect":
                return
            if connect.get("type") != "websocket.connect" or selected is None or self.stopping:
                raise _Close(1008)
            owner = (os.getpid(), asyncio.get_running_loop())
            if self.owner is not None and self.owner != owner:
                raise ConfigurationError("Subscriptions must stay in their owning process and loop.")
            self.owner = owner
            mount, config, identity = selected
            context = Request(scope, mount.resource.model, "get", identity)
            origin, host = context.header("origin"), context.header("host")
            if (origin not in config.origins or host not in config.hosts or scope.get("query_string", b"")
                    or SUBPROTOCOL not in scope.get("subprotocols", ())
                    or origin.startswith("https:") and scope.get("scheme") != "wss"):
                raise _Close(1008)
            try:
                identity = mount.resource.schema.parse_id(identity)
            except ValueError:
                raise _Close(1008) from None
            if sum(p == mount.path for p in self.tasks.values()) >= config.connections:
                raise _Close(1013)
            self.tasks[task] = mount.path
            context = Request(scope, mount.resource.model, "get", identity)

            async def current(expected=None):
                async with asyncio.timeout(config.auth_timeout):
                    principal = self.app.authenticate(context)
                    if inspect.isawaitable(principal):
                        principal = await principal
                    if principal is None or expected is not None and principal != expected:
                        raise _Close(1008)
                    if type(principal) is not Principal:
                        raise ConfigurationError("Authentication must return a Principal or None.")
                    authorized = mount.authorize(Request(scope, mount.resource.model, "get", identity, principal))
                    if inspect.isawaitable(authorized):
                        authorized = await authorized
                    if type(authorized) is not bool:
                        raise ConfigurationError("Authorization must return a boolean.")
                    if not authorized:
                        raise _Close(1008)
                    snapshot = await mount.resource.store.event_snapshot(identity)
                    if not RecordAccess(principal, mount.record_policy, mount.scan_limit).allows("get", snapshot.value):
                        raise _Close(1008)
                    return principal, snapshot

            principal, _ = await current()
            await emit({"type": "websocket.accept", "subprotocol": SUBPROTOCOL})
            async with asyncio.timeout(config.subscribe_timeout):
                initial = await receive()
            if initial.get("type") == "websocket.disconnect":
                return
            if initial.get("type") != "websocket.receive" or type(initial.get("text")) is not str or initial.get("bytes") is not None:
                raise _Close(1008)
            if len(initial["text"].encode("utf-8")) > config.max_message_bytes:
                raise _Close(1009)
            try:
                data = json.loads(initial["text"], object_pairs_hook=_pairs, parse_constant=_constant)
            except (ValueError, RecursionError):
                raise _Close(1008) from None
            if (type(data) is not dict or set(data) != {"v", "type", "cursor"} or type(data["v"]) is not int
                    or data["v"] != 1 or data["type"] != "subscribe"
                    or data["cursor"] is not None and not _cursor(data["cursor"])):
                raise _Close(1008)

            async def watch():
                message = await receive()
                if message.get("type") != "websocket.disconnect":
                    raise _Close(1008)

            async def stream():
                cursor, first = data["cursor"], True
                async with asyncio.timeout(config.lifetime):
                    while True:
                        _, snapshot = await current(principal)
                        page = None if cursor is None else await mount.resource.store.event_changes(
                            cursor, key=identity, limit=config.pending_limit)
                        if page is not None and page.resync:
                            await output({"v": 1, "type": "resync"})
                        if first or page is not None and page.changed:
                            await output({"v": 1, "type": "snapshot", "cursor": snapshot.cursor,
                                          "value": self.app._dump(mount.resource, snapshot.value)})
                        cursor, first = snapshot.cursor, False
                        await asyncio.sleep(config.poll_interval)

            owned = [asyncio.create_task(watch()), asyncio.create_task(stream())]
            done, _ = await asyncio.wait(owned, return_when=asyncio.FIRST_COMPLETED)
            if owned[0] in done:
                owned[0].result()
            else:
                owned[1].result()
        except asyncio.CancelledError:
            if not self.stopping:
                raise
            try:
                await emit({"type": "websocket.close", "code": 1001})
            except (OSError, _Close):
                pass
        except (NotFoundError, _Close, TimeoutError) as error:
            code = error.code if isinstance(error, _Close) else 1008
            try:
                await emit({"type": "websocket.close", "code": code})
            except (OSError, _Close):
                pass
        except OSError:
            pass
        except Exception as error:
            logger.error("Unhandled subscription error (%s).", type(error).__name__)
            try:
                await emit({"type": "websocket.close", "code": 1011})
            except (OSError, _Close):
                pass
        finally:
            for child in owned:
                child.cancel()
            if owned:
                await asyncio.gather(*owned, return_exceptions=True)
            self.tasks.pop(task, None)
