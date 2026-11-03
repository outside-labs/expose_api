"""Explicit read-only tabular schemas and policy-filtered registered pages."""

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import re
import types
from typing import Union, get_args, get_origin

from .access import RecordAccess
from .errors import ConfigurationError

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
KINDS = {str: "text", bool: "boolean", int: "integer", float: "number"}


@dataclass(frozen=True, slots=True)
class TableColumn:
    id: str
    field: str
    label: str
    sortable: bool = False
    searchable: bool = False

    def __post_init__(self):
        if (type(self.id) is not str or not ID.fullmatch(self.id) or type(self.field) is not str
                or type(self.label) is not str or len(self.label) > 256
                or type(self.sortable) is not bool or type(self.searchable) is not bool):
            raise ConfigurationError("Invalid tabular column declaration.")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class TabularProfile:
    def __init__(self, mount, *, table_id, schema_version, application_scope, columns, scan_limit=10000, max_work=1000000):
        if (any(type(value) is not str or not ID.fullmatch(value) for value in (table_id, schema_version, application_scope))
                or type(columns) not in (tuple, list) or not 1 <= len(columns) <= 64
                or any(not isinstance(column, TableColumn) for column in columns)
                or len({c.id for c in columns}) != len(columns) or len({c.field for c in columns}) != len(columns)
                or type(scan_limit) is not int or not 1 <= scan_limit <= 10000
                or type(max_work) is not int or not 1 <= max_work <= 1000000
                or "list" not in mount.operations or not callable(getattr(mount.resource.store, "tabular_page", None))):
            raise ConfigurationError("Select a bounded registered tabular source and list operation.")
        self.mount, self.table_id, self.schema_version = mount, table_id, schema_version
        self.application_scope, self.columns = application_scope, tuple(columns)
        self.scan_limit, self.max_work = min(scan_limit, mount.scan_limit), max_work
        self._public = []
        schema = mount.resource.schema
        for column in self.columns:
            spec = schema._contract.fields.get(column.field)
            if spec is None or spec.write_only or column.field not in mount.resource.store.query_fields:
                raise ConfigurationError("Tabular columns require declared visible registered fields.")
            annotation = spec.annotation
            args = get_args(annotation) if get_origin(annotation) in (Union, types.UnionType) else (annotation,)
            scalar = tuple(arg for arg in args if arg is not type(None))
            if len(scalar) != 1 or scalar[0] not in KINDS or column.searchable and scalar[0] is not str:
                raise ConfigurationError("Tabular columns require the portable scalar profile.")
            self._public.append(dict(id=column.id, label=column.label, type=KINDS[scalar[0]], nullable=type(None) in args,
                                     visible=True, sortable=column.sortable, searchable=column.searchable, writable=False))
        self._by_id = {c.id: c for c in self.columns}

    def schema(self):
        return dict(contract="tabular/1", table_id=self.table_id, schema_version=self.schema_version,
                    columns=[dict(c) for c in self._public],
                    limits=dict(page_rows=1000, page_cells=16000, search_fields=8, search_codepoints=256,
                                scan_rows=self.scan_limit, sqlite_instructions=self.max_work))

    def admit(self, data):
        from .asgi import _HTTPError
        try:
            if type(data) is not dict or data.keys() - {"generation", "columns", "order", "search", "filters", "limit", "cursor"}:
                raise ValueError
            generation, limit = data.get("generation"), data.get("limit", 50)
            if type(generation) is not int or not 0 <= generation <= 9007199254740991 or type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError
            selected = data.get("columns", list(self._by_id))
            if (type(selected) is not list or len(selected) > 64 or any(type(name) is not str or name not in self._by_id for name in selected)
                    or len(set(selected)) != len(selected) or len(selected) * limit > 16000):
                raise ValueError
            order, filters, search = data.get("order", []), data.get("filters", []), data.get("search")
            if type(order) is not list or len(order) > 3 or type(filters) is not list or len(filters) > 16:
                raise ValueError
            seen = set()
            for item in order:
                if (type(item) is not dict or item.keys() != {"column_id", "descending"}
                        or type(item["column_id"]) is not str or item["column_id"] not in self._by_id
                        or not self._by_id[item["column_id"]].sortable or type(item["descending"]) is not bool
                        or item["column_id"] in seen):
                    raise ValueError
                seen.add(item["column_id"])
            for item in filters:
                if (type(item) is not dict or item.keys() != {"column_id", "operator", "value"}
                        or type(item["column_id"]) is not str or item["column_id"] not in self._by_id
                        or item["operator"] not in ("eq", "ne", "lt", "le", "gt", "ge", "in", "is_null")):
                    raise ValueError
            if search is not None:
                if type(search) is not dict or search.keys() != {"fields", "term"}:
                    raise ValueError
                fields, term = search["fields"], search["term"]
                if (type(fields) is not list or not 1 <= len(fields) <= 8 or type(term) is not str or len(term) > 256
                        or any(type(name) is not str or name not in self._by_id or not self._by_id[name].searchable for name in fields)
                        or len(set(fields)) != len(fields)):
                    raise ValueError
                term.encode("utf-8")
                search = dict(fields=sorted(fields), term=term) if term else None
            cursor = data.get("cursor")
            if cursor is not None and (type(cursor) is not str or not 1 <= len(cursor) <= 16384):
                raise ValueError
            return dict(generation=generation, limit=limit, columns=selected, order=order, filters=filters, search=search, cursor=cursor)
        except (ValueError, TypeError, KeyError):
            raise _HTTPError(400, "query", "Invalid table query.") from None

    def _scope(self, query, principal):
        identity = None if principal is None else [principal.subject, principal.tenant, sorted(principal.scopes)]
        descriptor = [self.application_scope, self.table_id, self.schema_version, self.schema(), identity,
                      query["order"], sorted(query["filters"], key=canonical), query["search"]]
        return hashlib.sha256(canonical(descriptor).encode()).hexdigest()

    async def query(self, data, principal):
        from .asgi import _HTTPError
        query = self.admit(data)
        fingerprint = self._scope(query, principal)
        cursor = query["cursor"]
        if cursor is not None:
            try:
                value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if type(value) is not list or len(value) != 2 or value[0] != fingerprint or type(value[1]) is not str:
                    raise ValueError
                cursor = value[1]
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise _HTTPError(400, "invalid_cursor", "Invalid table cursor.") from None
        access = RecordAccess(principal, self.mount.record_policy, self.scan_limit) if self.mount.record_policy else None
        from backpack_store import Filter, InvalidCursorError, Order, QueryBudgetError, StorageError, TextSearch, ValidationError
        filters = tuple(Filter(self._by_id[f["column_id"]].field, f["operator"], f["value"]) for f in query["filters"])
        order = tuple(Order(self._by_id[o["column_id"]].field, o["descending"]) for o in query["order"])
        search = query["search"]
        search = TextSearch(tuple(self._by_id[name].field for name in search["fields"]), search["term"]) if search else None
        try:
            rows, continuation = await self.mount.resource.store.tabular_page(filters=filters, order=order, search=search,
                cursor=cursor, limit=query["limit"], predicate=(lambda value: access.allows("list", value)) if access else None,
                scan_limit=self.scan_limit, max_work=self.max_work)
        except InvalidCursorError:
            raise _HTTPError(400, "invalid_cursor", "Invalid table cursor.") from None
        except ValidationError:
            raise _HTTPError(400, "query", "Invalid table query.") from None
        except QueryBudgetError:
            raise _HTTPError(503, "budget_exceeded", "Table query exceeds the work budget.") from None
        except StorageError:
            raise _HTTPError(503, "storage_unavailable", "Table storage is unavailable.") from None
        next_cursor = base64.urlsafe_b64encode(canonical([fingerprint, continuation]).encode()).decode() if continuation else None
        if next_cursor and len(next_cursor) > 16384:
            raise _HTTPError(400, "invalid_cursor", "Table cursor exceeds the bound.")
        projected = []
        for row in rows:
            value = self.mount.resource.schema.dump(row.value)
            projected.append(dict(key=row.key, revision=row.revision,
                                  values={name: value[self._by_id[name].field] for name in query["columns"]}))
        return dict(contract="tabular/1", table_id=self.table_id, schema_version=self.schema_version,
                    generation=query["generation"], columns=query["columns"], rows=projected, next_cursor=next_cursor)

    async def dispatch(self, app, suffix, scope, receive, state):
        from .asgi import _Disconnected, _HTTPError, _response
        state.protected = self.mount.record_policy is not None
        method = scope["method"]
        methods = {"schema": ("GET", "HEAD", "OPTIONS"), "query": ("POST", "OPTIONS")}.get(suffix)
        if methods is None:
            raise _HTTPError(404, "not_found", "Route not found.")
        if method == "OPTIONS":
            return _response(204, headers=((b"allow", ", ".join(methods).encode()),))
        if method not in methods:
            raise _HTTPError(405, "method_not_allowed", "Method not allowed.", headers=((b"allow", ", ".join(methods).encode()),))
        context = await app._context(self.mount, scope, "list", None)
        if scope.get("query_string", b""):
            raise _HTTPError(400, "query", "Invalid table query parameters.")
        if suffix == "schema":
            return _response(200, self.schema())
        data = await app._body(scope, receive, max_size=65536)
        task = asyncio.create_task(self.query(data, context.principal))
        async def disconnected():
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                if event["type"] != "http.request":
                    raise _HTTPError(400, "query", "Invalid table request event.")
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher
                raise _Disconnected
            result = _response(200, await task)
            if len(result[2]) > 4194304:
                raise _HTTPError(503, "budget_exceeded", "Table response exceeds the byte budget.")
            return result
        finally:
            for pending in (task, watcher):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)


def openapi_paths(app):
    """Describe implemented routes using only visible declarations."""
    from copy import deepcopy
    from .access import BearerAuth
    def obj(properties, required=()):
        return dict(type="object", properties=properties, required=list(required), additionalProperties=False)
    paths = {}
    for prefix, profile in app._tables.items():
        ids = list(profile._by_id)
        column = dict(type="string", enum=ids)
        scalar = {"text": "string", "boolean": "boolean", "integer": "integer", "number": "number"}
        values = {}
        for item in profile._public:
            value = dict(type=[scalar[item["type"]], "null"] if item["nullable"] else scalar[item["type"]])
            if item["type"] == "integer":
                value.update(minimum=-9007199254740991, maximum=9007199254740991)
            values[item["id"]] = value
        generation = dict(type="integer", minimum=0, maximum=9007199254740991)
        cursor = dict(type=["string", "null"], minLength=1, maxLength=16384)
        search = obj({"fields": dict(type="array", items=column, minItems=1, maxItems=8, uniqueItems=True),
                      "term": dict(type="string", maxLength=256)}, ("fields", "term"))
        descriptor = obj(dict(generation=generation, columns=dict(type="array", items=column, maxItems=64, uniqueItems=True),
            order=dict(type="array", maxItems=3, items=obj({"column_id": column, "descending": {"type": "boolean"}}, ("column_id", "descending"))),
            filters=dict(type="array", maxItems=16, items=obj({"column_id": column,
                "operator": {"enum": ["eq", "ne", "lt", "le", "gt", "ge", "in", "is_null"]}, "value": {}}, ("column_id", "operator", "value"))),
            search={"anyOf": [search, {"type": "null"}]}, limit=dict(type="integer", minimum=1, maximum=1000, default=50), cursor=cursor), ("generation",))
        identity = dict(contract={"const": "tabular/1"}, table_id={"const": profile.table_id}, schema_version={"const": profile.schema_version})
        row = obj(dict(key=dict(type="string", minLength=1, maxLength=128), revision=dict(type="string", pattern="^[1-9][0-9]{0,18}$"),
                       values=obj(values)), ("key", "revision", "values"))
        page = obj(dict(**identity, generation=generation, columns=dict(type="array", items=column, maxItems=64, uniqueItems=True),
                        rows=dict(type="array", items=row, maxItems=1000), next_cursor=cursor),
                   (*identity, "generation", "columns", "rows", "next_cursor"))
        schema = obj(dict(**identity, columns=dict(type="array", maxItems=64, items={"enum": profile._public}),
                          limits={"const": profile.schema()["limits"]}), (*identity, "columns", "limits"))
        for suffix, method, result in (("schema", "get", schema), ("query", "post", page)):
            responses = {"200": {"description": "Detached tabular result", "content": {"application/json": {"schema": result}}}}
            for code in (400, 404, 405, 413, 415, 422, 500, 503):
                responses[str(code)] = {"description": "Value-free table error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
            operation = dict(operationId=f"table_{profile.table_id}_{suffix}", responses=responses,
                             **{"x-max-response-bytes": 4194304, "x-query-additional-properties": False,
                                "x-access-scan-limit": profile.scan_limit, "x-sqlite-work-limit": profile.max_work})
            if method == "post":
                operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": descriptor}}}
                operation["x-max-body-bytes"] = min(app.max_body_size, 65536)
            if profile.mount.authorize:
                responses["403"] = {"description": "Operation denied"}
            if profile.mount.record_policy:
                responses["401"] = {"description": "Authentication required", "headers": {"WWW-Authenticate": {"schema": {"type": "string"}}}}
                for response in responses.values():
                    response.setdefault("headers", {})["Cache-Control"] = {"schema": {"const": "no-store"}}
                operation["x-authentication"] = "caller-owned"
                if isinstance(app.authenticate, BearerAuth):
                    operation["security"] = [{"BearerAuth": []}]
            methods = {method: operation, "options": {"responses": {"204": {"description": "Allowed methods", "headers": {"Allow": {"schema": {"type": "string"}}}}}}}
            if profile.mount.record_policy:
                methods["options"]["responses"]["204"]["headers"]["Cache-Control"] = {"schema": {"const": "no-store"}}
            if method == "get":
                methods["head"] = deepcopy(operation)
                methods["head"]["operationId"] += "_head"
                for response in methods["head"]["responses"].values():
                    response.pop("content", None)
            paths[prefix + "/_table/" + suffix] = methods
    return paths
