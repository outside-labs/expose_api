"""Explicit compiled value validation beneath the API's model policies."""

from dataclasses import is_dataclass
import types
from typing import Union, get_origin

from enforce_type import SchemaError, ValidationError as ValueValidationError, compile_type

from .errors import ConfigurationError
from .models import MAX_DEPTH, ModelSchema, _Contract


class _CompiledContract(_Contract):
    def __init__(self, model, active):
        self._plans = {}
        super().__init__(model, active)

    def _check(self, annotation, active):
        super()._check(annotation, active)
        if get_origin(annotation) in (list, dict, Union, types.UnionType) or is_dataclass(annotation):
            return
        if annotation not in self._plans:
            try:
                self._plans[annotation] = compile_type(annotation)
            except SchemaError:
                raise ConfigurationError("The selected value engine cannot compile this API annotation.") from None

    def _value(self, annotation, value, path, depth, *, encode, include_write_only=False):
        plan = self._plans.get(annotation)
        if plan is not None and depth <= MAX_DEPTH:
            try:
                # The API accepts finite native integers for float output too.
                # JSON loading performs that public, non-coercing conversion.
                if annotation is float:
                    return plan.load(value)
                return plan.dump(value) if encode else plan.load(value)
            except ValueValidationError:
                # Retain the API's leaf error codes and ordering. The API also
                # validates enum Unicode after construction and literal text
                # after matching; a whole-model engine has different timing.
                pass
        return super()._value(annotation, value, path, depth, encode=encode,
                              include_write_only=include_write_only)


class EnforceTypeSchema[T](ModelSchema[T]):
    """Reuse published leaf plans while preserving the API's full model contract.

    No additional annotations, coercion, constraints or collection limits are
    selected. Plans compile during setup and are reused synchronously by CRUD,
    including atomic store transforms and protected principal views.
    """

    _contract_type = _CompiledContract
