"""Asynchronous storage contracts and an isolated, atomic in-memory store."""

from collections.abc import Callable
from copy import deepcopy
from itertools import islice
from threading import RLock
from typing import Protocol, runtime_checkable

from .errors import AccessScanError, ConflictError, NotFoundError


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


@runtime_checkable
class AccessStore[T](Store[T], Protocol):
    """Additional atomic capabilities required by protected resource views."""

    async def list_where(self, predicate: Callable[[T], bool], *, offset: int, limit: int, scan_limit: int) -> list[T]: ...
    async def delete_where(self, key: str, predicate: Callable[[T], bool]) -> None: ...


def _visible(values, predicate, *, offset, limit, scan_limit):
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000
            or type(scan_limit) is not int or not 1 <= scan_limit <= 10000):
        raise ValueError("Invalid bounded visible pagination.")
    result, visible = [], 0
    for index, value in enumerate(values):
        if index >= scan_limit:
            raise AccessScanError("Visible page exceeds the scan budget.")
        if predicate(deepcopy(value)):
            if visible >= offset:
                result.append(deepcopy(value))
                if len(result) == limit:
                    return result
            visible += 1
    return result


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

    async def list_where(self, predicate, *, offset=0, limit=100, scan_limit=10000):
        with self._lock:
            return _visible(self._records.values(), predicate, offset=offset, limit=limit, scan_limit=scan_limit)

    async def delete_where(self, key: str, predicate) -> None:
        with self._lock:
            if key not in self._records or not predicate(deepcopy(self._records[key])):
                raise NotFoundError("Record not found.")
            del self._records[key]
