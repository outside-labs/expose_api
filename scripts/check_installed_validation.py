"""Qualify explicitly selected compiled validation in an isolated installation."""

import argparse
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
from pathlib import Path
import platform
import sys
import unittest


def package_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"):
            digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--backpack", action="store_true")
    args = parser.parse_args()
    import expose_api
    import enforce_type
    from expose_api.enforce_type import EnforceTypeSchema

    for module in (expose_api, enforce_type):
        assert Path(module.__file__).is_relative_to(Path(sys.prefix))
    assert metadata.version("enforce-type") == "0.1.0"
    engine_digest = package_digest(Path(enforce_type.__file__).parent)
    # Published 0.1.0 wheel bytes, independent of platform and bytecode cache.
    assert engine_digest == "9292836eaed067a2feba0eceb0fd91c928c45871dfc153d4728e37ab4e0eb9fe"
    if not args.backpack:
        assert importlib.util.find_spec("backpack_store") is None
    sys.path.insert(0, str(args.tests))
    names = ["test_enforce_type.AdapterTests", "test_enforce_type_http.AdapterHTTPTests"]
    if args.backpack:
        names.append("test_enforce_type_http.AdapterPersistentTests")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt = {"profile": "enforce-type-api-v1", "python": platform.python_version(), "os": platform.system(), "machine": platform.machine(),
        "passed": result.wasSuccessful() and not result.skipped,
        "package_version": expose_api.__version__, "schema": EnforceTypeSchema.__name__,
        "engine": {"version": metadata.version("enforce-type"), "package_sha256": engine_digest},
        "backpack": args.backpack, "tests": result.testsRun, "skipped": len(result.skipped),
        "failures": len(result.failures), "errors": len(result.errors),
        "modules_sha256": {name: hashlib.sha256(Path(expose_api.__file__).with_name(name).read_bytes()).hexdigest()
                           for name in ("models.py", "resources.py", "asgi.py", "enforce_type.py")}}
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    assert receipt["passed"] and result.testsRun == (11 if args.backpack else 10), receipt


if __name__ == "__main__":
    main()
