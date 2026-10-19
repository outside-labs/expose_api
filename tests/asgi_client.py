"""Small event-level test driver; no networking or third-party client."""

import json
from dataclasses import dataclass


@dataclass
class Result:
    status: int
    headers: dict
    body: bytes
    messages: list
    reads: int

    @property
    def json(self):
        return json.loads(self.body)


async def request(app, path="/notes", method="GET", *, data=None, body=None, headers=None,
                  events=None, root_path="", query=b""):
    headers = list(headers or [])
    if data is not None:
        body = json.dumps(data).encode()
        headers.append((b"content-type", b"application/json"))
    pending = list(events) if events is not None else [{"type": "http.request", "body": body or b""}]
    messages, reads = [], 0
    async def receive():
        nonlocal reads
        reads += 1
        return pending.pop(0) if pending else {"type": "http.disconnect"}
    async def send(event):
        messages.append(event)
    await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.5"},
               "method": method, "path": path, "root_path": root_path, "headers": headers,
               "query_string": query, "http_version": "1.1"}, receive, send)
    if not messages:
        return Result(0, {}, b"", [], reads)
    assert [m["type"] for m in messages] == ["http.response.start", "http.response.body"]
    return Result(messages[0]["status"], dict(messages[0]["headers"]), messages[1]["body"], messages, reads)
