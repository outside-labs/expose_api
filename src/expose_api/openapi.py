"""OpenAPI 3.1.0 projection of registered runtime model contracts."""

import types
from copy import deepcopy
from enum import Enum
from typing import Literal, Union, get_args, get_origin
from uuid import UUID

from .errors import ConfigurationError
from .access import BearerAuth


def _annotation(annotation, contract, mode):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return {"anyOf": [_annotation(arg, contract, mode) for arg in args]}
    if origin is Literal:
        return {"enum": list(args)}
    if annotation in (str, bool, int, float, type(None)):
        result = {"type": {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}[annotation]}
        if annotation is int:
            result["x-runtime-strict-integer"] = True
        return result
    if annotation is UUID:
        return {"type": "string", "format": "uuid"}
    if origin is list:
        return {"type": "array", "items": _annotation(args[0], contract, mode)}
    if origin is dict:
        return {"type": "object", "additionalProperties": _annotation(args[1], contract, mode)}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"enum": [item.value for item in annotation]}
    nested = contract if annotation is contract.model else contract.nested[annotation]
    return _object(nested, "response" if mode == "response" else "create")


def _object(contract, mode, *, id_field=None):
    properties, required = {}, []
    for name, field in contract.fields.items():
        if (mode == "response" and field.write_only or mode != "response" and field.read_only
                or mode == "patch" and name == id_field):
            continue
        properties[name] = _annotation(field.annotation, contract, mode)
        if field.read_only:
            properties[name]["readOnly"] = True
        if field.write_only:
            properties[name]["writeOnly"] = True
        if mode == "response" or mode == "create" and field.required:
            required.append(name)
    result = {"type": "object", "properties": properties, "additionalProperties": False,
              "x-runtime-validation": True}
    if required:
        result["required"] = required
    return result


def model_schema(schema, *, mode="response"):
    if mode not in ("response", "create", "patch"):
        raise ConfigurationError("Choose a response, create or patch schema.")
    return _object(schema._contract, mode, id_field=schema.id_field)


ERROR_SCHEMA = {
    "type": "object", "required": ["error"], "additionalProperties": False,
    "properties": {"error": {
        "type": "object", "required": ["code", "message", "details"], "additionalProperties": False,
        "properties": {"code": {"type": "string"}, "message": {"type": "string"},
                       "details": {"type": "array", "items": {
                           "type": "object", "required": ["path", "code"], "additionalProperties": False,
                           "properties": {"path": {"type": "array", "items": {"type": ["string", "integer"]}},
                                          "code": {"type": "string"}}}}}}}}


def document(app, *, version="0.1.0", description=None):
    """Return a fresh document. No route, listener or credential policy is added."""
    if (type(app.title) is not str or type(version) is not str or not version
            or description is not None and type(description) is not str):
        raise ConfigurationError("OpenAPI version and description must be strings.")
    info = {"title": app.title, "version": version}
    if description is not None:
        info["description"] = description
    paths, schemas = {}, {}
    for index, mount in enumerate(app._mounts):
        schema = mount.resource.schema
        names = {mode: f"Resource{index}{mode.title()}" for mode in ("response", "create", "patch")}
        schemas.update({names[mode]: model_schema(schema, mode=mode) for mode in names})
        for suffix, methods in (("", {"get": "list", "post": "create"}),
                                ("/{identity}", {"get": "get", "patch": "update", "delete": "delete"})):
            operations = {}
            for method, operation in methods.items():
                if operation not in mount.operations:
                    continue
                success = "201" if operation == "create" else "204" if operation == "delete" else "200"
                responses = {success: {"description": "Success"}}
                if operation != "delete":
                    value = {"$ref": f"#/components/schemas/{names['response']}"}
                    if operation == "list":
                        value = {"type": "array", "items": value}
                    responses[success]["content"] = {"application/json": {"schema": value}}
                codes = {400, 404, 405, 422, 500}
                if mount.authorize is not None:
                    codes.add(403)
                if mount.record_policy is not None:
                    codes.add(401)
                    if operation == "list":
                        codes.add(503)
                if operation in ("create", "update"):
                    codes.update({409, 413, 415})
                for code in sorted(codes):
                    responses[str(code)] = {"description": "Request or application error", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
                value = {"operationId": f"resource_{index}_{operation}", "responses": responses,
                         "x-access-policy": "callback" if mount.authorize is not None else "public"}
                if mount.record_policy is not None:
                    value["x-access-policy"] = "authenticated-record-policy"
                    value["x-authentication"] = "caller-owned"
                    if isinstance(app.authenticate, BearerAuth):
                        value["security"] = [{"BearerAuth": []}]
                    if operation == "list":
                        value["x-access-scan-limit"] = mount.scan_limit
                    value["responses"]["401"]["headers"] = {"WWW-Authenticate": {"schema": {"type": "string"}}}
                parameters = []
                if suffix:
                    parameters.append({"name": "identity", "in": "path", "required": True,
                                       "schema": {"type": "string", "minLength": 1, "maxLength": 256},
                                       "description": "Canonical resource identity; validated by the registered model."})
                if operation == "list":
                    parameters += [{"name": name, "in": "query", "schema": {"type": "integer", "minimum": minimum,
                                    **({"maximum": maximum} if maximum else {}), "default": default}}
                                   for name, minimum, maximum, default in (("offset", 0, None, 0), ("limit", 1, 1000, 100))]
                    value["x-query-additional-properties"] = False
                if parameters:
                    value["parameters"] = parameters
                if operation in ("create", "update"):
                    mode = "create" if operation == "create" else "patch"
                    value["requestBody"] = {"required": True, "content": {"application/json": {
                        "schema": {"$ref": f"#/components/schemas/{names[mode]}"}}}}
                    value["x-max-body-bytes"] = app.max_body_size
                operations[method] = value
                if method == "get":
                    head = deepcopy(value)
                    head["operationId"] = f"resource_{index}_{operation}_head"
                    for response in head["responses"].values():
                        response.pop("content", None)
                    operations["head"] = head
            if operations:
                operations["options"] = {"operationId": f"resource_{index}_{'item' if suffix else 'collection'}_options",
                                         "responses": {"204": {"description": "Allowed methods", "headers": {
                                             "Allow": {"schema": {"type": "string"}}}}}}
                paths[mount.path + suffix] = operations
    schemas["Error"] = ERROR_SCHEMA
    components = {"schemas": schemas}
    if isinstance(app.authenticate, BearerAuth) and any(mount.record_policy is not None for mount in app._mounts):
        components["securitySchemes"] = {"BearerAuth": {"type": "http", "scheme": "bearer"}}
    return deepcopy({"openapi": "3.1.0", "info": info, "paths": paths, "components": components})
