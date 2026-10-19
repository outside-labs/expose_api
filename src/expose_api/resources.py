"""Transport-independent CRUD over typed dataclasses."""

from dataclasses import replace
from uuid import UUID

from .errors import ConfigurationError, Issue, ValidationError
from .models import ModelSchema
from .stores import Store


class Resource[T]:
    """Direct Python CRUD. HTTP access policies are enforced by App, not here."""

    def __init__(self, model: type[T], *, store: Store[T], id_field: str = "id"):
        if not isinstance(store, Store):
            raise ConfigurationError("A store must implement the asynchronous Store contract.")
        self.schema = ModelSchema(model, id_field=id_field)
        self.model, self.store = model, store

    def _key(self, identity: str | int | UUID) -> str:
        if type(identity) is not str and type(identity) is not self.schema.id_type:
            raise ValidationError(Issue((self.schema.id_field,), "identity"))
        return self.schema.parse_id(str(identity))

    async def create(self, data: dict) -> T:
        value = self.schema.create(data)
        return await self.store.create(self.schema.key(value), value)

    async def get(self, identity: str | int | UUID) -> T:
        return await self.store.get(self._key(identity))

    async def list(self, *, offset: int = 0, limit: int = 100) -> list[T]:
        if type(offset) is not int or offset < 0:
            raise ValidationError(Issue(("offset",), "range"))
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError(Issue(("limit",), "range"))
        return await self.store.list(offset=offset, limit=limit)

    async def update(self, identity: str | int | UUID, data: dict) -> T:
        key, changes = self._key(identity), self.schema.patch(data)

        def apply(current):
            try:
                updated = replace(current, **changes)
            except (TypeError, ValueError):
                raise ValidationError(Issue((), "constructor")) from None
            self.schema.dump(updated)
            if self.schema.key(updated) != key:
                raise ValidationError(Issue((self.schema.id_field,), "immutable"))
            return updated

        return await self.store.update(key, apply)

    async def delete(self, identity: str | int | UUID) -> None:
        await self.store.delete(self._key(identity))
