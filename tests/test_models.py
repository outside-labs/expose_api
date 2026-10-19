import unittest
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

from expose_api import ConfigurationError, ModelSchema, ValidationError


class Color(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Address:
    city: str
    secret: str = field(default="hidden", metadata={"expose_api": {"write_only": True}})


@dataclass(slots=True, frozen=True)
class Note:
    title: str
    content: str = ""
    id: UUID = field(default_factory=uuid4)
    address: Address | None = None
    tags: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    color: Color = Color.RED
    state: Literal["draft", "ready"] = "draft"
    server: int = field(default=1, metadata={"expose_api": {"read_only": True}})
    password: str = field(default="", metadata={"expose_api": {"write_only": True}})
    category: ClassVar[str] = "note"

    @property
    def preview(self):
        return self.title


@dataclass
class StringRecord:
    id: str


@dataclass
class IntegerRecord:
    id: int


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.schema = ModelSchema(Note)

    def test_nested_round_trip_and_output_fields(self):
        data = {"title": "hello", "address": {"city": "Paris", "secret": "token"}, "password": "secret",
                "scores": {"quality": 2}, "color": "blue", "tags": ["a"], "state": "ready"}
        note = self.schema.create(data)
        output = self.schema.dump(note)
        self.assertIsInstance(note.id, UUID)
        self.assertEqual(note.color, Color.BLUE)
        self.assertEqual(output["address"], {"city": "Paris"})
        self.assertEqual(output["scores"], {"quality": 2.0})
        self.assertEqual(output["id"], str(note.id))
        self.assertEqual(output["server"], 1)
        self.assertNotIn("password", output)
        self.assertNotIn("preview", output)
        self.assertNotIn("category", output)

    def test_factories_are_per_instance(self):
        a, b = self.schema.create({"title": "a"}), self.schema.create({"title": "b"})
        self.assertNotEqual(a.id, b.id)
        self.assertIsNot(a.tags, b.tags)

    def test_patch_is_partial_and_nested_values_replace(self):
        patch = self.schema.patch({"content": "new", "address": {"city": "Lyon"}})
        self.assertEqual(set(patch), {"content", "address"})
        self.assertEqual(patch["address"].city, "Lyon")
        self.assertEqual(self.schema.patch({}), {})
        with self.assertRaises(ValidationError):
            self.schema.patch({"id": str(uuid4())})

    def test_bad_values_are_rejected_without_values_in_errors(self):
        cases = [{}, {"title": 123}, {"title": None}, {"title": "x", "server": 3},
                 {"title": "x", "password-is-a-secret": "secret"}, {"title": "x", "tags": [False]},
                 {"title": "x", "scores": {"x": True}}, {"title": "x", "color": "green"},
                 {"title": "x", "state": False}, {"title": "x", "address": {"city": 3}},
                 {"title": "x", "scores": {"x": float("nan")}}, {"title": "\ud800"},
                 {"title": "x", "id": "bad"}, {"title": "x", "tags": ("a",)}]
        for data in cases:
            with self.subTest(data=repr(data)), self.assertRaises(ValidationError) as caught:
                self.schema.create(data)
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("secret", repr([issue.as_dict() for issue in caught.exception.issues]))

    def test_bad_defaults_and_output_are_validated(self):
        @dataclass
        class BadDefault:
            id: str = "ok"
            count: int = True
        with self.assertRaises(ValidationError):
            ModelSchema(BadDefault).create({})
        with self.assertRaises(ValidationError):
            self.schema.dump(Note(title=12))

    def test_identity_rules(self):
        schema = ModelSchema(StringRecord)
        for identity in ("", ".", "..", "a/b", "a\\b", "x\n", "\ud800", "a" * 257):
            with self.subTest(identity=repr(identity)), self.assertRaises(ValidationError):
                schema.create({"id": identity})
        self.assertEqual(schema.create({"id": "hello ?#%é"}).id, "hello ?#%é")
        schema = ModelSchema(IntegerRecord)
        for identity in (True, "1", 1.0):
            with self.assertRaises(ValidationError):
                schema.create({"id": identity})
        for identity in ("01", "+1", "1.0", "1 2"):
            with self.assertRaises(ValidationError):
                schema.parse_id(identity)
        self.assertEqual(schema.parse_id("-1"), "-1")

    def test_registration_rejects_unsupported_types_and_metadata(self):
        @dataclass
        class AnyRecord:
            id: str
            value: Any
        @dataclass
        class NoInit:
            id: str = field(init=False, default="x")
        @dataclass
        class WithInitVar:
            id: str
            extra: InitVar[int] = 1
        for model in (Note("x"), str, AnyRecord, NoInit, WithInitVar):
            with self.subTest(model=model), self.assertRaises(ConfigurationError):
                ModelSchema(model)
        for annotation in (dict, list, set[str], dict[int, str], object):
            @dataclass
            class Unsupported:
                id: str
                value: annotation
            with self.assertRaises(ConfigurationError):
                ModelSchema(Unsupported)
        for options in ({"readonly": True}, {"read_only": 1}, {"read_only": True, "write_only": True},
                        {"write_only": True}):
            @dataclass
            class BadMetadata:
                id: str = field(default="x", metadata={"expose_api": options})
            with self.assertRaises(ConfigurationError):
                ModelSchema(BadMetadata)

    def test_missing_readonly_default_is_rejected(self):
        @dataclass
        class Bad:
            id: str
            generated: str = field(metadata={"expose_api": {"read_only": True}})
        with self.assertRaises(ConfigurationError):
            ModelSchema(Bad)

    def test_recursive_model_is_rejected(self):
        @dataclass
        class Recursive:
            id: str
            child: "Recursive | None" = None
        Recursive.__annotations__["child"] = Recursive | None
        with self.assertRaises(ConfigurationError):
            ModelSchema(Recursive)

    def test_custom_identity_and_value_free_constructor_error(self):
        @dataclass
        class Record:
            key: int
            def __post_init__(self):
                if self.key < 0:
                    raise ValueError("private value")
        schema = ModelSchema(Record, id_field="key")
        self.assertEqual(schema.key(schema.create({"key": 1})), "1")
        with self.assertRaises(ValidationError) as caught:
            schema.create({"key": -1})
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
