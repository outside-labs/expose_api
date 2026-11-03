import json
from pathlib import Path
import unittest

from expose_api.asgi import _pairs


class TabularContractTests(unittest.TestCase):
    def test_finite_contract_examples_and_duplicate_json_refusal(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "docs/tabular-v1.json").read_text())
        self.assertEqual(fixture["contract"], "tabular/1")
        self.assertEqual({r["status"] for r in fixture["requests"]}, {200, 400, 404, 409, 422})
        for example in fixture["rejected_wire"]:
            if "json" in example:
                with self.assertRaises(ValueError):
                    json.loads(example["json"], object_pairs_hook=_pairs)
        for response in fixture["errors"]:
            self.assertEqual(set(response["error"]), {"code", "message", "details"})
            self.assertEqual(response["error"]["details"], [])
