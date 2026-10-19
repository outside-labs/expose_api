import runpy
import unittest
from pathlib import Path

from asgi_client import request


class ExampleTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_example(self):
        example = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples" / "notes.py"))
        created = await request(example["app"], method="POST", data={"title": "First note"})
        self.assertEqual(created.status, 201)
        self.assertEqual(created.json["content"], "")
        self.assertEqual((await request(example["app"])).json, [created.json])
        self.assertEqual((await example["notes"].get(created.json["id"])).title, "First note")
