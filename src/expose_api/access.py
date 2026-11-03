"""Caller-owned immutable identity and synchronous, detached record policies."""

import inspect
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

from .errors import AccessDeniedError, ConfigurationError, NotFoundError


def _identifier(value):
    return type(value) is str and 1 <= len(value) <= 256 and all(ord(c) >= 32 and ord(c) != 127 for c in value)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant: str | None = None
    scopes: frozenset[str] = frozenset()

    def __post_init__(self):
        if (not _identifier(self.subject) or self.tenant is not None and not _identifier(self.tenant)
                or type(self.scopes) is not frozenset or len(self.scopes) > 64
                or not all(_identifier(scope) for scope in self.scopes)):
            raise ConfigurationError("Principal identifiers and scopes must be bounded immutable strings.")


@dataclass(frozen=True, slots=True)
class RecordAccess:
    principal: Principal
    policy: Callable
    scan_limit: int = 10000

    def __post_init__(self):
        if type(self.principal) is not Principal or not callable(self.policy) or inspect.iscoroutinefunction(self.policy):
            raise ConfigurationError("Record access requires a Principal and synchronous policy.")
        if type(self.scan_limit) is not int or not 1 <= self.scan_limit <= 10000:
            raise ConfigurationError("scan_limit must be between 1 and 10000.")

    def allows(self, operation, value):
        result = self.policy(self.principal, operation, deepcopy(value))
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ConfigurationError("Record policies must return a boolean synchronously.")
        if type(result) is not bool:
            raise ConfigurationError("Record policies must return a boolean.")
        return result

    def check(self, operation, value, *, conceal=False):
        if not self.allows(operation, value):
            if conceal:
                raise NotFoundError("Record not found.")
            raise AccessDeniedError("Operation not permitted.")
