"""Typed dataclass applications with no runtime dependencies."""

__version__ = "0.1.0"

from .errors import ConfigurationError, ConflictError, Issue, NotFoundError, ValidationError
from .models import ModelSchema

__all__ = ["ConfigurationError", "ConflictError", "Issue", "ModelSchema", "NotFoundError", "ValidationError"]
