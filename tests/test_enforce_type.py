from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
import importlib.util
import unittest
from typing import Literal
from unittest.mock import patch
from uuid import UUID

from expose_api import ConfigurationError, ModelSchema, ValidationError

AVAILABLE = importlib.util.find_spec("enforce_type") is not None
if AVAILABLE:
    from expose_api.enforce_type import EnforceTypeSchema


class Color(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Child:
    text: str
    hidden: str = field(default="", metadata={"expose_api": {"write_only": True}})


@dataclass(frozen=True, slots=True)
class Record:
    id: UUID
    title: str
    count: int = 1
    score: float = 1
    enabled: bool = False
    color: Color = Color.RED
    state: Literal["ready", 1, False, None] = "ready"
    child: Child | None = None
    entries: list[dict[str, float | None]] = field(default_factory=list)
    server: int = field(default=1, metadata={"expose_api": {"read_only": True}})
    password: str = field(default="", metadata={"expose_api": {"write_only": True}})


IDENTITY = "00000000-0000-0000-0000-000000000001"


@unittest.skipUnless(AVAILABLE, "optional enforce-type profile not installed")
class AdapterTests(unittest.TestCase):
    def test_nested_values_projection_and_native_float_output_match_builtin(self):
        data = {"id": IDENTITY, "title": "Garden 🌱", "child": {"text": "nested", "hidden": "private"},
                "entries": [{"private-key": 2}, {"other": None}], "color": "blue", "password": "private"}
        schemas = [kind(Record) for kind in (ModelSchema, EnforceTypeSchema)]
        values = [schema.create(data) for schema in schemas]
        self.assertEqual(values[0], values[1])
        self.assertEqual(schemas[0].dump(values[0]), schemas[1].dump(values[1]))
        self.assertIs(type(schemas[1].dump(values[1])["score"]), float)
        self.assertEqual(schemas[1].dump(values[1])["child"], {"text": "nested"})
        self.assertNotIn("password", schemas[1].dump(values[1]))
        self.assertEqual(schemas[1].patch({"child": {"text": "replacement"}}), {"child": Child("replacement")})

    def test_invalid_input_issue_paths_codes_and_order_match_builtin(self):
        cases = [[], {"title": True}, {"id": "invalid"}, {"server": 3},
                 {"private-key": "private-value"}, {1: "private-value"}, {"count": True},
                 {"score": True}, {"score": float("nan")}, {"score": 10 ** 400},
                 {"color": "green"}, {"state": True}, {"title": "\ud800"},
                 {"entries": [{"private-key": True}]}, {"child": {"text": 2, "private-key": "private"}},
                 {"child": {"hidden": "private"}}, {"enabled": 1, "count": "private", "score": None}]
        for changes in cases:
            data = {"id": IDENTITY, "title": "valid", **changes} if type(changes) is dict else changes
            for operation in ("create", "patch"):
                if operation == "patch" and type(changes) is dict:
                    data = changes
                errors = []
                for kind in (ModelSchema, EnforceTypeSchema):
                    with self.subTest(changes=changes, operation=operation, schema=kind), self.assertRaises(ValidationError) as caught:
                        getattr(kind(Record), operation)(data)
                    errors.append([issue.as_dict() for issue in caught.exception.issues])
                    self.assertNotIn("private", repr(errors[-1]) + str(caught.exception))
                self.assertEqual(errors[0], errors[1])

    def test_missing_fields_and_empty_patch_keep_existing_contract(self):
        for kind in (ModelSchema, EnforceTypeSchema):
            with self.assertRaises(ValidationError) as caught:
                kind(Record).create({})
            self.assertEqual([issue.as_dict() for issue in caught.exception.issues],
                             [{"path": ["id"], "code": "required"}, {"path": ["title"], "code": "required"}])
            self.assertEqual(kind(Record).patch({}), {})

    def test_root_factories_partial_updates_and_constructor_failures(self):
        calls = []
        @dataclass
        class Constructed:
            id: str
            values: list[int] = field(default_factory=lambda: calls.append("factory") or [])
            def __post_init__(self):
                calls.append("constructor")
                if self.id == "refused":
                    raise ValueError("private-value")
        schema = EnforceTypeSchema(Constructed)
        self.assertEqual(schema.patch({}), {})
        self.assertEqual(calls, [])
        first, second = schema.create({"id": "one"}), schema.create({"id": "two"})
        self.assertIsNot(first.values, second.values)
        self.assertEqual(calls, ["factory", "constructor", "factory", "constructor"])
        with self.assertRaises(ValidationError) as caught:
            schema.create({"id": "refused"})
        self.assertEqual(caught.exception.issues[0].code, "constructor")
        self.assertNotIn("private", str(caught.exception))

    def test_unsupported_models_and_invalid_defaults_remain_refused(self):
        @dataclass
        class Computed:
            id: str
            count: int = field(init=False, default=1)
        @dataclass
        class Invalid:
            id: str = "one"
            count: int = True
        with self.assertRaises(ConfigurationError):
            EnforceTypeSchema(Computed)
        with self.assertRaises(ValidationError):
            EnforceTypeSchema(Invalid).create({})
        class Subclass(Invalid):
            pass
        with self.assertRaises(ValidationError):
            EnforceTypeSchema(Invalid).dump(Subclass())

    def test_plans_compile_once_and_support_concurrent_calls(self):
        import expose_api.enforce_type as adapter
        with patch.object(adapter, "compile_type", wraps=adapter.compile_type) as compile_plan:
            schema = EnforceTypeSchema(Record)
            count = compile_plan.call_count
            self.assertGreater(count, 0)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda index: schema.dump(schema.create({"id": IDENTITY, "title": str(index)})), range(40)))
            self.assertEqual([value["title"] for value in results], list(map(str, range(40))))
            self.assertEqual(compile_plan.call_count, count)

    def test_literal_unicode_and_enum_construction_keep_api_timing(self):
        calls = []
        class InvalidText(Enum):
            VALUE = "\ud800"
        @dataclass
        class Text:
            id: str
            literal: Literal["\ud800"]
        @dataclass
        class EnumText:
            id: str
            value: InvalidText
            def __post_init__(self):
                calls.append("constructed")
        for model, data in ((Text, {"id": "one", "literal": "\ud800"}),
                            (EnumText, {"id": "one", "value": "\ud800"})):
            errors = []
            for kind in (ModelSchema, EnforceTypeSchema):
                with self.assertRaises(ValidationError) as caught:
                    kind(model).create(data)
                errors.append(caught.exception.issues)
            self.assertEqual(errors[0], errors[1])
        self.assertEqual(calls, ["constructed", "constructed"])
