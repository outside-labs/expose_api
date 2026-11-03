"""Optional journal state using only public Backpack models and units of work."""

from dataclasses import dataclass, fields, replace
from uuid import uuid4

from backpack_store import DataclassCodec

from .errors import ConfigurationError
from .events import EventSnapshot, _event_key, _event_page


@dataclass(frozen=True)
class _Stream:
    id: str
    head: str
    position: int
    retention: int
    resource_type: str
    resource_table: str


@dataclass(frozen=True)
class _Change:
    id: str
    position: int
    previous: str
    cursor: str
    key: str


class JournalModel:
    """Every mutating call must run inside the owner's outer unit of work."""

    def __init__(self, resource, stream, changes):
        self.resource, self.stream, self.changes = resource, stream, changes

    @classmethod
    def configure(cls, storage, resource, namespace, retention, resource_type, resource_table):
        def register(model, suffix):
            return storage.register(DataclassCodec(model, type_key=f"expose_api.events.{suffix}"),
                namespace=namespace, table=f"app_{namespace}_{suffix}", primary_key="id",
                columns={field.name: field.name for field in fields(model)})
        stream, changes = register(_Stream, "state"), register(_Change, "changes")
        storage.initialize(namespace)
        with storage.unit_of_work():
            rows = stream.list(limit=1)
            if not rows:
                stream.create(_Stream("state", uuid4().hex, 0, retention, resource_type, resource_table))
            elif (rows[0].value.retention, rows[0].value.resource_type, rows[0].value.resource_table) != (retention, resource_type, resource_table):
                raise ConfigurationError("The event namespace belongs to another resource or retention profile.")
        return cls(resource, stream, changes)

    def _append(self, key):
        _event_key(key)
        state = self.stream.get("state")
        value = state.value
        # The registered SQLite scalar profile accepts exact JSON integers.
        if value.position >= 9_007_199_254_740_991:
            raise ConfigurationError("Event position is exhausted; select a new stream namespace.")
        position, cursor = value.position + 1, uuid4().hex
        slot = f"slot-{value.position % value.retention:04}"
        change = _Change(slot, position, value.head, cursor, key)
        if value.position < value.retention:
            self.changes.create(change)
        else:
            row = self.changes.get(slot)
            self.changes.replace(slot, change, expected_revision=row.revision)
        self.stream.replace("state", replace(value, head=cursor, position=position), expected_revision=state.revision)

    def create(self, value):
        row = self.resource.create(value)
        self._append(row.key)
        return row

    def get(self, key):
        return self.resource.get(key)

    def list(self, **kwargs):
        return self.resource.list(**kwargs)

    def find(self, **kwargs):
        return self.resource.find(**kwargs)

    def update(self, key, **kwargs):
        row = self.resource.update(key, **kwargs)
        self._append(key)
        return row

    def delete(self, key, **kwargs):
        self.resource.delete(key, **kwargs)
        self._append(key)

    def snapshot(self, key):
        _event_key(key)
        return EventSnapshot(self.resource.get(key).value, self.stream.get("state").value.head)

    def page(self, cursor, key, limit):
        state, rows, after = self.stream.get("state").value, [], None
        while True:
            chunk = self.changes.list(after=after, limit=1000)
            rows.extend(row.value for row in chunk)
            if len(chunk) < 1000:
                break
            after = chunk[-1].key
        rows.sort(key=lambda row: row.position)
        return _event_page(state.head, ((row.previous, row.cursor, row.key) for row in rows), cursor, key, limit)
