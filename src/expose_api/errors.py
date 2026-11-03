"""Stable public errors with value-free validation details."""

from dataclasses import dataclass


class ConfigurationError(ValueError):
    """An application or model cannot be configured as requested."""


class AccessDeniedError(PermissionError):
    """A proposed record is forbidden by the caller's value policy."""


class AccessScanError(RuntimeError):
    """The finite visible-list scan cannot complete the requested page."""


@dataclass(frozen=True, slots=True)
class Issue:
    path: tuple[str | int, ...]
    code: str

    def as_dict(self) -> dict:
        return {"path": list(self.path), "code": self.code}


class ValidationError(ValueError):
    """Input or output does not match a model; submitted values are omitted."""

    def __init__(self, *issues: Issue):
        self.issues = issues
        super().__init__("Data does not match the model.")


class NotFoundError(LookupError):
    """The requested record does not exist."""


class ConflictError(ValueError):
    """A record with this identity already exists."""


class RevisionConflictError(ConflictError):
    """A checked mutation used a stale committed revision."""
