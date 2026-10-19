"""Native ASGI 3 application with explicit dataclass resource routes."""

import inspect
import json
import logging
import math
import re
import sys
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote

from .errors import ConfigurationError, ConflictError, NotFoundError, ValidationError
from .models import MAX_DEPTH
from .resources import Resource

logger = logging.getLogger("expose_api")
OPERATIONS = frozenset({"list", "get", "create", "update", "delete"})


class ProtocolError(RuntimeError):
    """The ASGI server supplied an invalid protocol event."""


class _Disconnected(Exception):
    pass


class _HTTPError(Exception):
    def __init__(self, status, code, message, *, headers=()):
        self.status, self.code, self.message, self.headers = status, code, message, headers


@dataclass(frozen=True, slots=True)
class Request:
    """Authorization context; identity is a canonical key, not a fetched row."""

    scope: dict
    model: type
    operation: str
    identity: str | None

    def header(self, name: str) -> str | None:
        values = [v.decode("latin-1") for k, v in self.scope.get("headers", ()) if k.lower() == name.lower().encode("ascii")]
        return values[0] if len(values) == 1 else None


@dataclass(frozen=True, slots=True)
class _Mount:
    path: str
    resource: Resource
    operations: frozenset[str]
    authorize: Callable | None


@asynccontextmanager
async def _empty_lifespan(app):
    yield


def _response(status, data=None, headers=()):
    if status == 204:
        return status, list(headers), b""
    body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return status, [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *headers], body


def _error(status, code, message, *, details=(), headers=()):
    return _response(status, {"error": {"code": code, "message": message, "details": list(details)}}, headers)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite JSON number.")


def _check_json(value):
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > MAX_DEPTH or type(node) is float and not math.isfinite(node):
            raise ValueError("Invalid JSON depth or number.")
        if isinstance(node, dict):
            pending.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            pending.extend((v, depth + 1) for v in node)


class App:
    """A dependency-free ASGI app. Include resources before the first ASGI call."""

    def __init__(self, *, title: str = "Expose API", max_body_size: int = 1024 * 1024, lifespan=None):
        if type(max_body_size) is not int or max_body_size < 1:
            raise ConfigurationError("max_body_size must be a positive integer.")
        self.title, self.max_body_size = title, max_body_size
        self.state: dict = {}
        self._lifespan_factory = lifespan or _empty_lifespan
        self._mounts: list[_Mount] = []
        self._frozen = False

    def run(self, *, host: str = "127.0.0.1", port: int = 8000) -> None:
        """Run the standard-library development HTTP server until interrupted."""
        from .server import run
        run(self, host=host, port=port)

    def include[T](self, model: type[T], *, store, path: str | None = None, id_field: str = "id",
                   operations: Iterable[str] = OPERATIONS, public: bool = False, authorize=None) -> Resource[T]:
        if self._frozen:
            raise ConfigurationError("Register resources before serving the application.")
        if type(public) is not bool or (public and authorize is not None) or (not public and not callable(authorize)):
            raise ConfigurationError("Choose public=True or an authorization callback.")
        resource = Resource(model, store=store, id_field=id_field)
        if path is None:
            path = "/" + re.sub(r"(?<!^)(?=[A-Z])", "_", model.__name__).lower() + "s"
        if type(path) is not str or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path):
            raise ConfigurationError("Resource paths must contain static URL segments without a trailing slash.")
        if isinstance(operations, str):
            raise ConfigurationError("Operations must be a collection of operation names.")
        selected = frozenset(operations)
        if not selected or selected - OPERATIONS:
            raise ConfigurationError("Select supported CRUD operations.")
        for mount in self._mounts:
            if mount.resource.model is model or mount.resource.store is store:
                raise ConfigurationError("Each resource requires its own model and store.")
            if path == mount.path or path.startswith(mount.path + "/") or mount.path.startswith(path + "/"):
                raise ConfigurationError("Resource paths cannot overlap.")
        self._mounts.append(_Mount(path, resource, selected, authorize))
        return resource

    async def __call__(self, scope, receive, send):
        self._frozen = True
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise ProtocolError("Unsupported ASGI scope type.")
        try:
            response = await self._dispatch(scope, receive)
        except _Disconnected:
            return
        except ProtocolError:
            raise
        except _HTTPError as exc:
            response = _error(exc.status, exc.code, exc.message, headers=exc.headers)
        except ValidationError as exc:
            response = _error(422, "validation", "Data does not match the model.",
                              details=[issue.as_dict() for issue in exc.issues])
        except NotFoundError:
            response = _error(404, "not_found", "Record not found.")
        except ConflictError:
            response = _error(409, "conflict", "Record already exists.")
        except Exception as exc:
            # Exception messages and tracebacks may contain credentials supplied to user code.
            logger.error("Unhandled application error (%s).", type(exc).__name__)
            response = _error(500, "internal", "Internal server error.")
        status, headers, body = response
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})

    async def _dispatch(self, scope, receive):
        path, root = scope["path"], scope.get("root_path", "").rstrip("/")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):] or "/"
        for mount in self._mounts:
            if path == mount.path:
                identity = None
                choices = {"GET": "list", "POST": "create"}
                break
            if path.startswith(mount.path + "/") and "/" not in path[len(mount.path) + 1:]:
                identity = path[len(mount.path) + 1:]
                if not identity:
                    continue
                choices = {"GET": "get", "PATCH": "update", "DELETE": "delete"}
                break
        else:
            raise _HTTPError(404, "not_found", "Route not found.")
        enabled = {method: operation for method, operation in choices.items() if operation in mount.operations}
        if not enabled:
            raise _HTTPError(404, "not_found", "Route not found.")
        allowed = sorted({*enabled, "OPTIONS", *({"HEAD"} if "GET" in enabled else set())})
        allow = ((b"allow", ", ".join(allowed).encode()),)
        method = scope["method"]
        if method == "OPTIONS":
            return _response(204, headers=allow)
        operation = enabled.get("GET" if method == "HEAD" else method)
        if operation is None:
            raise _HTTPError(405, "method_not_allowed", "Method not allowed.", headers=allow)
        if identity is not None:
            try:
                identity = mount.resource.schema.parse_id(identity)
            except ValidationError:
                raise _HTTPError(400, "identity", "Invalid record identity.") from None
        if mount.authorize is not None:
            permitted = mount.authorize(Request(dict(scope), mount.resource.model, operation, identity))
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if type(permitted) is not bool:
                raise ConfigurationError("Authorization callbacks must return a boolean.")
            if not permitted:
                raise _HTTPError(403, "forbidden", "Operation not permitted.")
        pagination = self._query(scope.get("query_string", b""), operation)
        resource = mount.resource
        if operation == "list":
            values = await resource.list(**pagination)
            return _response(200, [self._dump(resource, value) for value in values])
        if operation == "get":
            return _response(200, self._dump(resource, await resource.get(identity)))
        if operation == "delete":
            await resource.delete(identity)
            return _response(204)
        data = await self._body(scope, receive)
        if operation == "create":
            value = await resource.create(data)
            location = quote(root + mount.path, safe="/") + "/" + quote(resource.schema.key(value), safe="")
            return _response(201, self._dump(resource, value), ((b"location", location.encode("ascii")),))
        return _response(200, self._dump(resource, await resource.update(identity, data)))

    @staticmethod
    def _dump(resource, value):
        try:
            return resource.schema.dump(value)
        except ValidationError:
            raise RuntimeError("Stored record does not match its model.") from None

    @staticmethod
    def _query(raw, operation):
        try:
            if len(raw) > 8192 or re.search(rb"%(?![0-9a-fA-F]{2})", raw):
                raise ValueError
            pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=16)
            values = {}
            for key, value in pairs:
                if operation != "list" or key not in ("offset", "limit") or key in values:
                    raise ValueError
                if not re.fullmatch(r"[0-9]{1,10}", value):
                    raise ValueError
                values[key] = int(value)
            if values.get("offset", 0) < 0 or not 1 <= values.get("limit", 100) <= 1000:
                raise ValueError
            return values
        except (ValueError, UnicodeError):
            raise _HTTPError(400, "query", "Invalid query parameters.") from None

    async def _body(self, scope, receive):
        headers = scope.get("headers", ())
        content_types = [v.decode("latin-1") for k, v in headers if k.lower() == b"content-type"]
        if len(content_types) != 1:
            raise _HTTPError(415, "media_type", "A JSON Content-Type is required.")
        media, *parameters = content_types[0].lower().split(";")
        media = media.strip()
        if media != "application/json" and not re.fullmatch(r"application/[a-z0-9!#$&^_.+-]+\+json", media):
            raise _HTTPError(415, "media_type", "A JSON Content-Type is required.")
        if any(p.strip().startswith("charset=") and p.strip()[8:].strip('" ') not in ("utf-8", "utf8")
               for p in parameters):
            raise _HTTPError(415, "media_type", "JSON must use UTF-8.")
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        length = None
        if lengths:
            if len(lengths) != 1 or not re.fullmatch(rb"[0-9]{1,20}", lengths[0]):
                raise _HTTPError(400, "length", "Invalid Content-Length.")
            length = int(lengths[0])
            if length > self.max_body_size:
                raise _HTTPError(413, "body_too_large", "Request body exceeds the size limit.")
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                raise _Disconnected
            if event["type"] != "http.request" or type(event.get("body", b"")) is not bytes:
                raise ProtocolError("Expected an HTTP request event.")
            chunk = event.get("body", b"")
            if len(body) + len(chunk) > self.max_body_size:
                raise _HTTPError(413, "body_too_large", "Request body exceeds the size limit.")
            body.extend(chunk)
            if not event.get("more_body", False):
                break
        if length is not None and len(body) != length:
            raise _HTTPError(400, "length", "Content-Length does not match the request body.")
        try:
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            _check_json(data)
        except (ValueError, UnicodeError, RecursionError):
            raise _HTTPError(400, "json", "Invalid JSON body.") from None
        return data

    async def _lifespan(self, receive, send):
        event = await receive()
        if event["type"] != "lifespan.startup":
            raise ProtocolError("Expected lifespan.startup.")
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(self._lifespan_factory(self))
        except Exception as exc:
            logger.error("Application startup failed (%s).", type(exc).__name__)
            await send({"type": "lifespan.startup.failed", "message": "Application startup failed."})
            return
        try:
            await send({"type": "lifespan.startup.complete"})
            event = await receive()
            if event["type"] != "lifespan.shutdown":
                raise ProtocolError("Expected lifespan.shutdown.")
        except BaseException:
            await stack.__aexit__(*sys.exc_info())
            raise
        try:
            await stack.aclose()
        except Exception as exc:
            logger.error("Application shutdown failed (%s).", type(exc).__name__)
            await send({"type": "lifespan.shutdown.failed", "message": "Application shutdown failed."})
        else:
            await send({"type": "lifespan.shutdown.complete"})
