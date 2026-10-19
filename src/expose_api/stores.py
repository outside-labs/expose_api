"""Asynchronous storage contracts and an isolated, atomic in-memory store."""

from collections.abc import Callable
from copy import deepcopy
from itertools import islice
from threading import RLock
from typing import Protocol, runtime_checkable

from .errors import ConflictError, NotFoundError


@runtime_checkable
class Store[T](Protocol):
    """Stores return detached values and apply update transforms atomically.

    A transform is synchronous and must run inside the backend's transaction.
    If it raises, the stored record must remain unchanged. Implementations must
    not expose a live stored object to a caller or transform.
    """

    async def create(self, key: str, value: T) -> T: ...
    async def get(self, key: str) -> T: ...
    async def list(self, *, offset: int, limit: int) -> list[T]: ...
    async def update(self, key: str, transform: Callable[[T], T]) -> T: ...
    async def delete(self, key: str) -> None: ...


class MemoryStore[T]:
    """Insertion-ordered, process-local storage with no persistence or eviction."""

    def __init__(self):
        self._records: dict[str, T] = {}
        self._lock = RLock()

    async def create(self, key: str, value: T) -> T:
        with self._lock:
            if key in self._records:
                raise ConflictError("Record already exists.")
            stored = deepcopy(value)
            result = deepcopy(stored)
            self._records[key] = stored
            return result

    async def get(self, key: str) -> T:
        with self._lock:
            if key not in self._records:
                raise NotFoundError("Record not found.")
            return deepcopy(self._records[key])

    async def list(self, *, offset: int = 0, limit: int = 100) -> list[T]:
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("Pagination requires offset >= 0 and limit between 1 and 1000.")
        with self._lock:
            if offset >= len(self._records):
                return []
            return deepcopy(list(islice(self._records.values(), offset, offset + limit)))

    async def update(self, key: str, transform: Callable[[T], T]) -> T:
        with self._lock:
            if key not in self._records:
                raise NotFoundError("Record not found.")
            value = transform(deepcopy(self._records[key]))
            stored = deepcopy(value)
            result = deepcopy(stored)
            self._records[key] = stored
            return result

    async def delete(self, key: str) -> None:
        with self._lock:
            if key not in self._records:
                raise NotFoundError("Record not found.")
            del self._records[key]
