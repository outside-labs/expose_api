"""Explicit optional adapter for the qualified Backpack SQLite profile."""

import inspect
import re
from copy import deepcopy
from dataclasses import fields

from .errors import AccessDeniedError, AccessScanError, ConfigurationError, ConflictError, Issue, NotFoundError, RevisionConflictError, ValidationError
from .models import ModelSchema
from .stores import _visible
from .events import _event_limit

try:
    from backpack_store import (
        AlreadyExistsError, AsyncApplicationStore, CodecError, ConstraintError,
        NotFoundError as MissingRow, RevisionConflictError as StaleRow, TransactionError, ValidationError as InvalidRow,
    )
    from backpack_store.enforce_type_codec import EnforceTypeCodec
except ImportError:
    raise ImportError("Install expose-api[backpack] with its qualified development revision.") from None


class BackpackStore[T]:
    """A scalar dataclass resource on one bounded, thread-owned SQLite facade."""

    def __init__(self, backend, schema, *, events=0):
        self._backend, self.schema = backend, schema
        self._events = events

    @property
    def events_enabled(self):
        return self._events > 0

    @classmethod
    async def open(cls, model: type[T], path, *, namespace: str, table: str,
                   type_key: str, id_field: str = "id", schema_version: int = 1,
                   columns=None, unique=(), timeout: float = 5.0, queue_limit: int = 64,
                   events: int = 0, event_namespace: str | None = None):
        _event_limit(events, allow_disabled=True)
        if (events and (type(event_namespace) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", event_namespace)
                       or event_namespace == namespace) or not events and event_namespace is not None):
            raise ConfigurationError("Events require a distinct explicit namespace of at most 40 lowercase identifier characters.")
        schema = ModelSchema(model, id_field=id_field)
        if schema.id_type is not str:
            raise ConfigurationError("The Backpack resource profile requires a string domain key.")
        codec = EnforceTypeCodec(model, type_key=type_key, schema_version=schema_version)
        columns = dict(columns) if columns is not None else {field.name: field.name for field in fields(model)}
        def configure(storage):
            registered = storage.register(codec, namespace=namespace, table=table,
                                          primary_key=id_field, columns=columns, unique=unique)
            storage.initialize(namespace)
            if events:
                from .backpack_events import JournalModel
                return JournalModel.configure(storage, registered, event_namespace, events, type_key, table)
            return registered
        backend = await AsyncApplicationStore.open(path, configure=configure, timeout=timeout, queue_limit=queue_limit)
        return cls(backend, schema, events=events)

    async def _run(self, callback, *, transactional=False):
        try:
            return await self._backend.run(callback, transactional=transactional or self.events_enabled)
        except MissingRow:
            raise NotFoundError("Record not found.") from None
        except StaleRow:
            raise RevisionConflictError("Record changed after the expected revision.") from None
        except (AlreadyExistsError, ConstraintError):
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

    async def event_snapshot(self, key):
        if not self.events_enabled:
            raise ConfigurationError("Events are not enabled for this store.")
        return await self._run(lambda model: model.snapshot(key), transactional=True)

    async def event_changes(self, cursor, *, key, limit=64):
        if not self.events_enabled:
            raise ConfigurationError("Events are not enabled for this store.")
        return await self._run(lambda model: model.page(cursor, key, limit), transactional=True)

    async def list(self, *, offset: int = 0, limit: int = 100) -> list[T]:
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValidationError(Issue(("offset",), "range"))
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError(Issue(("limit",), "range"))
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

    async def update_checked(self, key: str, *, transform, expected_revision: str | None = None, predicate=None):
        """Check current access, then revision and transform in one transaction."""
        self._revision(expected_revision)
        if not callable(transform) or inspect.iscoroutinefunction(transform):
            raise ValueError("Storage transforms must be synchronous.")
        if predicate is not None and (not callable(predicate) or inspect.iscoroutinefunction(predicate)):
            raise ValueError("Record predicates must be synchronous.")
        validation_issues, access_errors = [], []
        def update(model):
            row = model.get(key)
            if predicate is not None:
                try:
                    self._check_predicate(predicate, row.value)
                except (AccessDeniedError, NotFoundError) as error:
                    access_errors.append(type(error))
                    raise
            if expected_revision is not None and row.revision != expected_revision:
                raise StaleRow("Record changed after the expected revision.")
            def checked(value):
                try:
                    result = transform(value)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise InvalidRow("Storage transforms must be synchronous.")
                    return self._value(key, result)
                except ValidationError as error:
                    # The storage boundary redacts arbitrary callback failures.
                    # Retain only the API's value-free issues until rollback finishes.
                    validation_issues.extend(error.issues)
                    raise
                except (AccessDeniedError, NotFoundError) as error:
                    access_errors.append(type(error))
                    raise
            return model.update(key, expected_revision=row.revision, transform=checked)
        try:
            return await self._run(update, transactional=True)
        except TransactionError:
            if validation_issues:
                raise ValidationError(*validation_issues) from None
            if access_errors:
                raise access_errors[0]("Record access denied.") from None
            raise

    async def _access_run(self, callback):
        errors = []
        def protected(model):
            try:
                return callback(model)
            except (AccessDeniedError, AccessScanError, NotFoundError) as error:
                errors.append(type(error))
                raise
        try:
            return await self._run(protected, transactional=True)
        except TransactionError:
            if errors:
                raise errors[0]("Record access could not complete.") from None
            raise

    async def list_where(self, predicate, *, offset=0, limit=100, scan_limit=10000):
        def page(model):
            def values():
                after, fetched = None, 0
                while fetched <= scan_limit:
                    rows = model.list(after=after, limit=min(1000, scan_limit + 1 - fetched))
                    if not rows:
                        return
                    for row in rows:
                        yield row.value
                    fetched += len(rows)
                    after = rows[-1].key
            return _visible(values(), predicate, offset=offset, limit=limit, scan_limit=scan_limit)
        return await self._access_run(page)

    @staticmethod
    def _check_predicate(predicate, value):
        allowed = predicate(deepcopy(value))
        if inspect.isawaitable(allowed):
            if inspect.iscoroutine(allowed):
                allowed.close()
            raise InvalidRow("Record predicates must be synchronous.")
        if type(allowed) is not bool:
            raise InvalidRow("Record predicates must return a boolean.")
        if not allowed:
            raise NotFoundError("Record not found.")

    async def delete_where(self, key: str, predicate, *, expected_revision: str | None = None) -> None:
        """Authorize before an optional revision check and delete atomically."""
        self._revision(expected_revision)
        if not callable(predicate) or inspect.iscoroutinefunction(predicate):
            raise ValueError("Record predicates must be synchronous.")
        def delete(model):
            row = model.get(key)
            self._check_predicate(predicate, row.value)
            if expected_revision is not None and row.revision != expected_revision:
                raise StaleRow("Record changed after the expected revision.")
            model.delete(key, expected_revision=row.revision)
        await self._access_run(delete)

    async def delete(self, key: str) -> None:
        await self.delete_checked(key)

    @staticmethod
    def _revision(revision):
        if revision is not None and (type(revision) is not str or not re.fullmatch(r"[1-9][0-9]{0,18}", revision)
                                     or int(revision) > 9223372036854775807):
            raise ValidationError(Issue(("revision",), "range"))

    async def delete_checked(self, key: str, *, expected_revision: str | None = None) -> None:
        """Delete atomically, optionally checking the caller's committed revision."""
        self._revision(expected_revision)
        def delete(model):
            row = model.get(key)
            if expected_revision is not None and row.revision != expected_revision:
                raise StaleRow("Record changed after the expected revision.")
            model.delete(key, expected_revision=row.revision)
        await self._run(delete, transactional=True)

    async def aclose(self) -> None:
        await self._backend.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()
