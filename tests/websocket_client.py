"""Bounded event-level WebSocket driver shared by installed acceptance."""

import asyncio
import json

from expose_api.websocket import SUBPROTOCOL


class Socket:
    def __init__(self, app, path="/notes/a", *, headers=None, root_path="", query=b"", send=None):
        self.app = app
        self.scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.5"},
                      "scheme": "ws", "path": path, "root_path": root_path, "query_string": query,
                      "headers": headers if headers is not None else [(b"origin", b"http://localhost"), (b"host", b"localhost")],
                      "subprotocols": [SUBPROTOCOL]}
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.send = send or self.outgoing.put
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.app(self.scope, self.incoming.get, self.send))
        await self.incoming.put({"type": "websocket.connect"})
        return await self.next()

    async def subscribe(self, cursor=None):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps({"v": 1, "type": "subscribe", "cursor": cursor})})

    async def next(self):
        event = await asyncio.wait_for(self.outgoing.get(), 3)
        if event["type"] == "websocket.send":
            return json.loads(event["text"])
        return event

    async def close(self):
        if self.task is not None:
            if self.task.cancelled():
                return
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(self.task, 3)


def register(app, resource, **kwargs):
    app.subscribe(resource, origins=("http://localhost",), hosts=("localhost",),
                  poll_interval=0.01, send_timeout=0.1, auth_timeout=0.1, subscribe_timeout=0.1, **kwargs)
