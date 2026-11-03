"""Explicit optional adapter for the qualified Backpack SQLite profile."""

import inspect
from copy import deepcopy
from dataclasses import fields

from .errors import ConfigurationError, ConflictError, Issue, NotFoundError, ValidationError
from .models import ModelSchema

try:
    from backpack_store import (
        AlreadyExistsError, AsyncApplicationStore, CodecError, ConstraintError,
        NotFoundError as MissingRow, RevisionConflictError, ValidationError as InvalidRow,
    )
    from backpack_store.enforce_type_codec import EnforceTypeCodec
except ImportError:
    raise ImportError("Install expose-api[backpack] with its qualified development revision.") from None


class BackpackStore[T]:
    """A scalar dataclass resource on one bounded, thread-owned SQLite facade."""

    def __init__(self, backend, schema):
        self._backend, self.schema = backend, schema

    @classmethod
    async def open(cls, model: type[T], path, *, namespace: str, table: str,
                   type_key: str, id_field: str = "id", schema_version: int = 1,
                   columns=None, unique=(), timeout: float = 5.0, queue_limit: int = 64):
        schema = ModelSchema(model, id_field=id_field)
        if schema.id_type is not str:
            raise ConfigurationError("The Backpack resource profile requires a string domain key.")
        codec = EnforceTypeCodec(model, type_key=type_key, schema_version=schema_version)
        columns = dict(columns) if columns is not None else {field.name: field.name for field in fields(model)}
        def configure(storage):
            registered = storage.register(codec, namespace=namespace, table=table,
                                          primary_key=id_field, columns=columns, unique=unique)
            storage.initialize(namespace)
            return registered
        backend = await AsyncApplicationStore.open(path, configure=configure, timeout=timeout, queue_limit=queue_limit)
        return cls(backend, schema)

    async def _run(self, callback, *, transactional=False):
        try:
            return await self._backend.run(callback, transactional=transactional)
        except MissingRow:
            raise NotFoundError("Record not found.") from None
        except (AlreadyExistsError, ConstraintError, RevisionConflictError):
            raise ConflictError("Record conflicts with committed state.") from None
        except (InvalidRow, CodecError):
            raise ValidationError(Issue((), "storage_model")) from None

    def _value(self, key, value):
        self.schema.dump(value)
        if self.schema.key(value) != key:
            raise ValidationError(Issue((self.schema.id_field,), "immutable"))
        return value

    async def create(self, key: str, value: T) -> T:
        # Detach before asynchronous admission as well as after decoding.
        value = deepcopy(value)
        self._value(key, value)
        return await self._run(lambda model: model.create(value).value)

    async def get(self, key: str) -> T:
        return (await self.get_row(key)).value

    async def get_row(self, key: str):
        """Return a detached value and its storage revision/envelope identity."""
        return await self._run(lambda model: model.get(key))

    async def list(self, *, offset: int = 0, limit: int = 100) -> list[T]:
        if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Persistent pagination requires offset 0–10000 and limit 1–1000.")
        def page(model):
            after, remaining = None, offset
            while remaining:
                rows = model.list(after=after, limit=min(remaining, 1000))
                if not rows:
                    return []
                after, remaining = rows[-1].key, remaining - len(rows)
            return [row.value for row in model.list(after=after, limit=limit)]
        return await self._run(page, transactional=True)

    async def update(self, key: str, transform) -> T:
        return (await self.update_checked(key, transform=transform)).value

    async def update_checked(self, key: str, *, transform, expected_revision: str | None = None):
        """Transform under a write transaction, optionally checking a caller revision."""
        if not callable(transform) or inspect.iscoroutinefunction(transform):
            raise ValueError("Storage transforms must be synchronous.")
        def update(model):
            row = model.get(key)
            if expected_revision is not None and row.revision != expected_revision:
                raise RevisionConflictError("Record changed after the expected revision.")
            def checked(value):
                result = transform(value)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise InvalidRow("Storage transforms must be synchronous.")
                return self._value(key, result)
            return model.update(key, expected_revision=row.revision, transform=checked)
        return await self._run(update, transactional=True)

    async def delete(self, key: str) -> None:
        def delete(model):
            row = model.get(key)
            model.delete(key, expected_revision=row.revision)
        await self._run(delete, transactional=True)

    async def aclose(self) -> None:
        await self._backend.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()
