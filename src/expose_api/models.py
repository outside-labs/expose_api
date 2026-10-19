"""Strict JSON contracts for ordinary, nonrecursive dataclasses."""

import inspect
import math
import re
import types
from dataclasses import MISSING, Field, dataclass, fields, is_dataclass
from enum import Enum
from typing import Literal, Union, get_args, get_origin, get_type_hints
from uuid import UUID

from .errors import ConfigurationError, Issue, ValidationError

MAX_DEPTH = 32


def _fail(path: tuple, code: str = "type"):
    raise ValidationError(Issue(path, code))


def _text(value: str, path: tuple) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(path, "unicode")
    return value


@dataclass(frozen=True)
class _Field:
    field: Field
    annotation: object
    read_only: bool = False
    write_only: bool = False

    @property
    def required(self) -> bool:
        return self.field.default is MISSING and self.field.default_factory is MISSING


class _Contract:
    def __init__(self, model: type, active: frozenset[type]):
        if model in active:
            raise ConfigurationError("Recursive dataclasses are not supported.")
        if not model.__dataclass_params__.init:
            raise ConfigurationError("Models require a dataclass constructor.")
        active = active | {model}
        try:
            hints = get_type_hints(model)
        except (NameError, TypeError) as exc:
            raise ConfigurationError("Model annotations must be resolvable.") from exc
        self.model = model
        self.fields: dict[str, _Field] = {}
        self.nested: dict[type, _Contract] = {}
        for field in fields(model):
            if not field.init:
                raise ConfigurationError("init=False fields are not supported.")
            annotation = hints[field.name]
            self._check(annotation, active)
            options = field.metadata.get("expose_api", {})
            if not isinstance(options, dict) or options.keys() - {"read_only", "write_only"}:
                raise ConfigurationError("Unknown expose_api field metadata.")
            if any(type(value) is not bool for value in options.values()):
                raise ConfigurationError("Field metadata flags must be booleans.")
            spec = _Field(field, annotation, **options)
            if spec.read_only and (spec.write_only or spec.required):
                raise ConfigurationError("Read-only fields require defaults and cannot be write-only.")
            self.fields[field.name] = spec
        signature = inspect.signature(model)
        if set(signature.parameters) != set(self.fields) or any(
            parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
            for parameter in signature.parameters.values()
        ):
            raise ConfigurationError("InitVars and custom constructor signatures are unsupported.")

    def _check(self, annotation, active):
        if annotation in (str, bool, int, float, type(None), UUID):
            return
        origin, args = get_origin(annotation), get_args(annotation)
        if origin in (Union, types.UnionType):
            for arg in args:
                self._check(arg, active)
            return
        if origin is Literal and args and all(type(arg) in (str, int, bool, type(None)) for arg in args):
            return
        if origin is list and len(args) == 1:
            self._check(args[0], active)
            return
        if origin is dict and len(args) == 2 and args[0] is str:
            self._check(args[1], active)
            return
        if isinstance(annotation, type):
            if issubclass(annotation, Enum) and len(annotation) and all(
                type(item.value) in (str, int) for item in annotation
            ):
                return
            if is_dataclass(annotation):
                self.nested[annotation] = _Contract(annotation, active)
                return
        raise ConfigurationError("Unsupported model annotation.")

    def values(self, data, path=(), depth=0, *, partial=False):
        if depth > MAX_DEPTH:
            _fail(path, "depth")
        if type(data) is not dict:
            _fail(path, "object")
        result, issues = {}, []
        for name, value in data.items():
            spec = self.fields.get(name) if type(name) is str else None
            if spec is None or spec.read_only:
                # Unknown keys can themselves contain submitted secrets.
                issues.append(Issue(path, "unknown_field" if spec is None else "read_only"))
                continue
            try:
                result[name] = self.convert(spec.annotation, value, (*path, name), depth + 1)
            except ValidationError as exc:
                issues.extend(exc.issues)
        if not partial:
            for name, spec in self.fields.items():
                if spec.required and name not in data:
                    issues.append(Issue((*path, name), "required"))
        if issues:
            raise ValidationError(*issues)
        return result

    def construct(self, data, path=(), depth=0):
        kwargs = self.values(data, path, depth)
        try:
            value = self.model(**kwargs)
        except (TypeError, ValueError):
            _fail(path, "constructor")
        self.encode(self.model, value, path, depth, include_write_only=True)
        return value

    def convert(self, annotation, value, path, depth):
        return self._value(annotation, value, path, depth, encode=False)

    def encode(self, annotation, value, path=(), depth=0, *, include_write_only=False):
        return self._value(annotation, value, path, depth, encode=True, include_write_only=include_write_only)

    def _value(self, annotation, value, path, depth, *, encode, include_write_only=False):
        if depth > MAX_DEPTH:
            _fail(path, "depth")
        origin, args = get_origin(annotation), get_args(annotation)
        if origin in (Union, types.UnionType):
            for arg in args:
                try:
                    return self._value(arg, value, path, depth, encode=encode, include_write_only=include_write_only)
                except ValidationError:
                    pass
            _fail(path)
        if origin is Literal:
            if not any(type(value) is type(arg) and value == arg for arg in args):
                _fail(path, "literal")
            return _text(value, path) if type(value) is str else value
        if annotation in (str, bool, int, type(None)):
            if type(value) is not annotation:
                _fail(path)
            return _text(value, path) if annotation is str else value
        if annotation is float:
            if type(value) not in (int, float):
                _fail(path)
            try:
                number = float(value)
            except OverflowError:
                _fail(path, "finite")
            if not math.isfinite(number):
                _fail(path, "finite")
            return number
        if annotation is UUID:
            if encode:
                if not isinstance(value, UUID):
                    _fail(path)
                return str(value)
            if type(value) is not str:
                _fail(path)
            try:
                return UUID(value)
            except ValueError:
                _fail(path, "uuid")
        if origin is list:
            if type(value) is not list:
                _fail(path)
            return [self._value(args[0], v, (*path, i), depth + 1, encode=encode, include_write_only=include_write_only)
                    for i, v in enumerate(value)]
        if origin is dict:
            if type(value) is not dict or any(type(key) is not str for key in value):
                _fail(path)
            return {_text(key, path): self._value(args[1], v, (*path, "*"), depth + 1, encode=encode,
                                                include_write_only=include_write_only)
                    for key, v in value.items()}
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            if encode:
                if not isinstance(value, annotation):
                    _fail(path)
                return _text(value.value, path) if type(value.value) is str else value.value
            for item in annotation:
                if type(value) is type(item.value) and value == item.value:
                    return item
            _fail(path, "enum")
        if annotation is self.model or annotation in self.nested:
            contract = self if annotation is self.model else self.nested[annotation]
            if not encode:
                return contract.construct(value, path, depth)
            if type(value) is not annotation:
                _fail(path)
            result = {}
            for name, spec in contract.fields.items():
                encoded = contract.encode(spec.annotation, getattr(value, name), (*path, name), depth + 1,
                                          include_write_only=include_write_only)
                if include_write_only or not spec.write_only:
                    result[name] = encoded
            return result
        raise ConfigurationError("Unsupported model annotation.")


class ModelSchema[T]:
    """Validate create/patch input, serialize records, and normalize identities."""

    def __init__(self, model: type[T], *, id_field: str = "id"):
        if not isinstance(model, type) or not is_dataclass(model):
            raise ConfigurationError("Include a dataclass type.")
        self.model, self.id_field = model, id_field
        self._contract = _Contract(model, frozenset())
        identity = self._contract.fields.get(id_field)
        if identity is None or identity.annotation not in (str, int, UUID) or identity.write_only:
            raise ConfigurationError("Identity must be a readable str, int, or UUID field.")
        self.id_type = identity.annotation

    def create(self, data: dict) -> T:
        result = self._contract.construct(data)
        self.key(result)
        return result

    def patch(self, data: dict) -> dict:
        if type(data) is dict and self.id_field in data:
            _fail((self.id_field,), "immutable")
        return self._contract.values(data, partial=True)

    def dump(self, value: T) -> dict:
        result = self._contract.encode(self.model, value)
        self.key(value)
        return result

    def key(self, value: T) -> str:
        identity = getattr(value, self.id_field)
        self._contract.encode(self.id_type, identity, (self.id_field,))
        return self.parse_id(str(identity))

    def parse_id(self, value: str) -> str:
        path = (self.id_field,)
        if type(value) is not str or not value or len(value) > 256:
            _fail(path, "identity")
        _text(value, path)
        if self.id_type is UUID:
            try:
                return str(UUID(value))
            except ValueError:
                _fail(path, "identity")
        if self.id_type is int:
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
                _fail(path, "identity")
            return str(int(value))
        if value in (".", "..") or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in value):
            _fail(path, "identity")
        return value
