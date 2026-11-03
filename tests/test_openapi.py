from dataclasses import dataclass, field
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4
import json
import unittest

from expose_api import App, ConfigurationError, MemoryStore
from asgi_client import request


class State(Enum):
    OPEN = "open"
    DONE = "done"


@dataclass
class Detail:
    label: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Document:
    title: str
    detail: Detail
    secret: str = field(metadata={"expose_api": {"write_only": True}})
    generated: str = field(default="synthetic-private-default", metadata={"expose_api": {"read_only": True}})
    state: State = State.OPEN
    label: Literal["first", "second"] = "first"
    metadata: dict[str, int | None] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)


class OpenAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = App(title="Documents")
        self.app.include(Document, store=MemoryStore(), path="/documents", public=True)

    async def test_route_allowlists_and_schema_match_actual_json(self):
        spec = self.app.openapi(version="2.0", description="Synthetic documents")
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertEqual(set(spec["paths"]["/documents"]), {"get", "head", "post", "options"})
        self.assertEqual(set(spec["paths"]["/documents/{identity}"]), {"get", "head", "patch", "delete", "options"})
        schemas = spec["components"]["schemas"]
        create, patch, response = [schemas["Resource0" + name] for name in ("Create", "Patch", "Response")]
        self.assertNotIn("generated", create["properties"])
        self.assertNotIn("secret", response["properties"])
        self.assertNotIn("id", patch["properties"])
        self.assertNotIn("required", patch)
        self.assertEqual(create["required"], ["title", "detail", "secret"])
        self.assertEqual(patch["properties"]["detail"]["required"], ["label"])
        self.assertFalse(create["additionalProperties"])
        self.assertEqual(response["properties"]["state"]["enum"], ["open", "done"])
        self.assertNotIn("synthetic-private-default", json.dumps(spec))
        created = await request(self.app, "/documents", "POST", data={"title": "Example", "detail": {"label": "Nested"}, "secret": "test"})
        self.assertEqual(created.status, 201)
        self.assertEqual(set(created.json), set(response["properties"]))
        invalid = await request(self.app, "/documents/" + created.json["id"], "PATCH", data={"detail": {}})
        self.assertEqual(invalid.status, 422)
        self.assertEqual(set(invalid.json), set(schemas["Error"]["properties"]))
        head = spec["paths"]["/documents"]["head"]
        self.assertNotIn("content", head["responses"]["200"])

    async def test_selected_methods_policy_and_no_automatic_document_endpoint(self):
        app = App()
        app.include(Document, store=MemoryStore(), path="/private", operations={"get"}, authorize=lambda request: False)
        spec = app.openapi()
        self.assertNotIn("/private", spec["paths"])
        get = spec["paths"]["/private/{identity}"]["get"]
        self.assertEqual(get["x-access-policy"], "callback")
        self.assertIn("403", get["responses"])
        self.assertNotIn("securitySchemes", spec["components"])
        self.assertEqual((await request(app, "/openapi.json")).status, 404)

    def test_documents_are_fresh_and_do_not_run_model_defaults(self):
        @dataclass
        class Record:
            id: str
            value: str = field(default_factory=lambda: self.fail("schema generation executed a factory"))
        app = App()
        app.include(Record, store=MemoryStore(), public=True)
        first = app.openapi()
        first["components"]["schemas"]["Error"]["properties"].clear()
        self.assertIn("error", app.openapi()["components"]["schemas"]["Error"]["properties"])
        with self.assertRaises(ConfigurationError):
            app.openapi(version=1)
