"""Bounded committed invalidations; values are always read from current state."""

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class EventSnapshot[T]:
    value: T
    cursor: str


@dataclass(frozen=True, slots=True)
class EventPage:
    cursor: str
    changed: bool
    resync: bool = False


@runtime_checkable
class EventStore[T](Protocol):
    """Atomic detached snapshots and bounded, coalesced key invalidations."""

    @property
    def events_enabled(self) -> bool: ...
    async def event_snapshot(self, key: str) -> EventSnapshot[T]: ...
    async def event_changes(self, cursor: str, *, key: str, limit: int = 64) -> EventPage: ...


def _event_limit(value, *, allow_disabled=False):
    if type(value) is not int or not (0 if allow_disabled else 1) <= value <= 4096:
        raise ConfigurationError("Event retention must be an integer between 1 and 4096, or 0 to disable.")
    return value


def _event_key(key):
    if type(key) is not str or not 1 <= len(key) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("Event keys must be bounded strings.")


def _cursor(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _event_page(head, changes, cursor, key, limit):
    """Each retained entry is (previous cursor, next cursor, affected key)."""
    _event_key(key)
    if not _cursor(cursor) or type(limit) is not int or not 1 <= limit <= 1024:
        raise ValueError("Invalid event cursor or pending-change limit.")
    if cursor == head:
        return EventPage(head, False)
    found, pending = False, 0
    for previous, current, changed_key in changes:
        if previous == cursor:
            found = True
        if found and changed_key == key:
            pending += 1
    if not found or pending > limit:
        return EventPage(head, True, True)
    return EventPage(head, pending > 0)
