"""Qualify the locked optional event profile from an isolated installation."""

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    import expose_api
    import backpack_store

    assert Path(expose_api.__file__).is_relative_to(Path(sys.prefix))
    assert Path(backpack_store.__file__).is_relative_to(Path(sys.prefix))
    backend = json.loads(metadata.distribution("backpack-store").read_text("direct_url.json"))["vcs_info"]["commit_id"]
    assert backend == "54f5a1e883fcc32f8d87cc553dc83dae0434300d"
    assert metadata.version("enforce-type") == "0.1.0"
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.discover(str(args.tests), pattern=pattern) for pattern in (
        "test_events.py", "test_websocket.py", "test_backpack*.py", "test*access.py", "test_persistent_websocket.py"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt = {"profile": "backpack-events-v1", "python": platform.python_version(), "os": platform.system(),
               "package_version": expose_api.__version__, "backend_commit": backend,
               "enforce_type_version": metadata.version("enforce-type"), "tests": result.testsRun,
               "skipped": len(result.skipped), "failures": len(result.failures), "errors": len(result.errors),
               "modules_sha256": {name: hashlib.sha256(Path(expose_api.__file__).with_name(name).read_bytes()).hexdigest()
                                  for name in ("events.py", "websocket.py", "backpack.py", "backpack_events.py")}}
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    assert result.wasSuccessful() and not result.skipped and result.testsRun >= 40, receipt


if __name__ == "__main__":
    main()
