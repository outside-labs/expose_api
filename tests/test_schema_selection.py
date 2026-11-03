from dataclasses import dataclass
import unittest

from expose_api import App, ConfigurationError, MemoryStore, ModelSchema, Principal, Resource, ValidationError

from asgi_client import request


@dataclass
class Record:
    id: str
    title: str = ""


class SelectedSchema(ModelSchema):
    def patch(self, data):
        changes = super().patch(data)
        if changes.get("title") == "refused":
            raise ValidationError()
        return changes


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_and_protected_operations_retain_selection(self):
        schema = SelectedSchema(Record)
        resource = Resource(Record, store=MemoryStore(), schema=schema)
        view = resource.for_principal(Principal("owner"), lambda principal, operation, value: True)
        self.assertIs(view.schema, schema)
        await view.create({"id": "one"})
        with self.assertRaises(ValidationError):
            await view.update("one", {"title": "refused"})
        self.assertEqual((await resource.get("one")).title, "")
        self.assertEqual((await view.update("one", {"title": "accepted"})).title, "accepted")

    async def test_http_registration_and_openapi_use_selected_contract(self):
        app = App()
        schema = SelectedSchema(Record)
        resource = app.include(Record, store=MemoryStore(), schema=schema, path="/records", public=True)
        self.assertIs(resource.schema, schema)
        default = App()
        default.include(Record, store=MemoryStore(), path="/records", public=True)
        self.assertEqual(app.openapi(), default.openapi())
        self.assertEqual((await request(app, "/records", "POST", data={"id": "one"})).status, 201)
        self.assertEqual((await request(app, "/records/one", "PATCH", data={"title": "refused"})).status, 422)

    def test_invalid_schema_selection_is_rejected_before_registration(self):
        @dataclass
        class Other:
            id: str
        @dataclass
        class Keys:
            id: str
            key: str
        for model, schema in ((Record, object()), (Record, ModelSchema(Other)),
                              (Keys, ModelSchema(Keys, id_field="key"))):
            with self.subTest(model=model, schema=schema):
                app = App()
                with self.assertRaises(ConfigurationError):
                    app.include(model, store=MemoryStore(), schema=schema, public=True)
                self.assertEqual(app.openapi()["paths"], {})
