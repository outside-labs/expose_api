"""Transport-independent CRUD over typed dataclasses."""

from dataclasses import replace
from uuid import UUID

from .access import RecordAccess
from .errors import ConfigurationError, Issue, ValidationError
from .models import ModelSchema
from .stores import AccessStore, Store


class Resource[T]:
    """Direct Python CRUD. HTTP access policies are enforced by App, not here."""

    def __init__(self, model: type[T], *, store: Store[T], id_field: str = "id", schema: ModelSchema[T] | None = None):
        if not isinstance(store, Store):
            raise ConfigurationError("A store must implement the asynchronous Store contract.")
        if schema is not None and (not isinstance(schema, ModelSchema) or schema.model is not model
                                   or schema.id_field != id_field):
            raise ConfigurationError("Select a ModelSchema with the same model and identity field.")
        self.schema = ModelSchema(model, id_field=id_field) if schema is None else schema
        self.model, self.store = model, store
        self._access = None

    def for_principal(self, principal, policy, *, scan_limit=10000):
        """Return a separate protected view; the original remains trusted access."""
        access = RecordAccess(principal, policy, scan_limit)
        if not isinstance(self.store, AccessStore):
            raise ConfigurationError("Protected resources require atomic AccessStore capabilities.")
        view = Resource(self.model, store=self.store, id_field=self.schema.id_field, schema=self.schema)
        view._access = access
        return view

    def _key(self, identity: str | int | UUID) -> str:
        if type(identity) is not str and type(identity) is not self.schema.id_type:
            raise ValidationError(Issue((self.schema.id_field,), "identity"))
        return self.schema.parse_id(str(identity))

    async def create(self, data: dict) -> T:
        value = self.schema.create(data)
        if self._access:
            self._access.check("create", value)
        return await self.store.create(self.schema.key(value), value)

    async def get(self, identity: str | int | UUID) -> T:
        value = await self.store.get(self._key(identity))
        if self._access:
            self._access.check("get", value, conceal=True)
        return value

    async def list(self, *, offset: int = 0, limit: int = 100) -> list[T]:
        if type(offset) is not int or offset < 0:
            raise ValidationError(Issue(("offset",), "range"))
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError(Issue(("limit",), "range"))
        if self._access:
            return await self.store.list_where(lambda value: self._access.allows("list", value),
                offset=offset, limit=limit, scan_limit=self._access.scan_limit)
        return await self.store.list(offset=offset, limit=limit)

    async def update(self, identity: str | int | UUID, data: dict) -> T:
        key, changes = self._key(identity), self.schema.patch(data)

        def apply(current):
            if self._access:
                self._access.check("update", current, conceal=True)
            try:
                updated = replace(current, **changes)
            except (TypeError, ValueError):
                raise ValidationError(Issue((), "constructor")) from None
            self.schema.dump(updated)
            if self.schema.key(updated) != key:
                raise ValidationError(Issue((self.schema.id_field,), "immutable"))
            if self._access:
                self._access.check("update", updated)
            return updated

        return await self.store.update(key, apply)

    async def delete(self, identity: str | int | UUID) -> None:
        if self._access:
            await self.store.delete_where(self._key(identity), lambda value: self._access.allows("delete", value))
            return
        await self.store.delete(self._key(identity))
