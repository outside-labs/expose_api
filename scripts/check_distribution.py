"""Build, inspect and install the exact candidate distributions without uploading."""

import argparse
import email.parser
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = {"__init__.py", "access.py", "asgi.py", "backpack.py", "backpack_events.py", "enforce_type.py", "errors.py", "events.py", "models.py", "openapi.py", "py.typed", "resources.py", "server.py", "stores.py", "websocket.py"}


def run(*args, cwd=ROOT):
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def metadata(raw, project):
    message = email.parser.BytesParser().parsebytes(raw)
    for key, expected in (("Name", "expose-api"), ("Version", project["version"]),
                          ("Requires-Python", ">=3.14"), ("Summary", project["description"]),
                          ("Description-Content-Type", "text/markdown")):
        assert message.get_all(key) == [expected], (key, message.get_all(key))
    requirements = message.get_all("Requires-Dist") or []
    expected = {"enforce-type==0.1.0; extra == 'enforce-type'",
                "backpack-store[enforce-type] @ git+https://github.com/outside-labs/backpack.git@54f5a1e883fcc32f8d87cc553dc83dae0434300d; extra == 'backpack'"}
    normalized = {"; ".join(part.strip() for part in value.replace('"', "'").split(";")) for value in requirements}
    assert len(requirements) == 2 and normalized == expected, requirements
    assert set(message.get_all("Provides-Extra")) == {"backpack", "enforce-type"}
    assert message.get_payload().replace("\r\n", "\n").strip() == (ROOT / "README.md").read_text(encoding="utf-8").strip()


def wheel_files(path, version, project):
    info = f"expose_api-{version}.dist-info"
    expected = {f"expose_api/{name}" for name in MODULES} | {f"{info}/{name}" for name in ("METADATA", "WHEEL", "RECORD")}
    with zipfile.ZipFile(path) as archive:
        files = [name for name in archive.namelist() if not name.endswith("/")]
        assert len(files) == len(set(files)) and set(files) == expected, files
        result = {name: archive.read(name) for name in files}
    for name in MODULES:
        assert result[f"expose_api/{name}"] == (ROOT / "src" / "expose_api" / name).read_bytes(), name
    metadata(result[f"{info}/METADATA"], project)
    return result


def installed(artifact, work, name, version, *, requirements=None):
    environment = work / name
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run("uv", "pip", "install", "--python", python, "--no-deps", artifact, cwd=work)
    if requirements is not None:
        run("uv", "pip", "install", "--python", python, "-r", requirements, cwd=work)
    probe = (
        "import importlib.metadata as m, pathlib, sys, expose_api; "
        f"assert m.version('expose-api') == expose_api.__version__ == {version!r}; "
        "assert all('extra ==' in r for r in m.requires('expose-api')); "
        "assert pathlib.Path(expose_api.__file__).is_relative_to(pathlib.Path(sys.prefix)); "
        "assert pathlib.Path(expose_api.__file__).with_name('py.typed').is_file(); "
        "assert not any(k in sys.modules for k in ('starlette','fastapi','pydantic','hidden_moves','backpack_store','enforce_type'))"
    )
    run(python, "-I", "-c", probe, cwd=work)
    if requirements is not None:
        receipt = work / f"{name}.json"
        run(python, "-I", "-W", "error::ResourceWarning", ROOT / "scripts" / "check_installed_events.py",
            "--tests", work / "tests", "--receipt", receipt, cwd=work)
        return json.loads(receipt.read_text(encoding="utf-8"))
    run(python, "-I", "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", work / "tests", "-v", cwd=work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--backpack", action="store_true", help="Qualify the locked optional event profile in both artifacts.")
    parser.add_argument("--receipt-file", type=Path)
    args = parser.parse_args()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    assert project["dependencies"] == []
    if args.tag and args.tag != f"v{version}":
        parser.error("Tag does not match the package version.")
    if args.output_dir and args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("Output directory must be new or empty.")
    with tempfile.TemporaryDirectory(prefix="expose-api-distribution-") as directory:
        work = Path(directory)
        artifacts = work / "artifacts"
        run("uv", "build", "--no-sources", "--out-dir", artifacts)
        wheel = artifacts / f"expose_api-{version}-py3-none-any.whl"
        source = artifacts / f"expose_api-{version}.tar.gz"
        original_wheel = wheel_files(wheel, version, project)
        prefix = f"expose_api-{version}"
        expected = {f"{prefix}/src/expose_api/{name}" for name in MODULES} | {
            f"{prefix}/{name}" for name in ("pyproject.toml", "pyproject.toml.orig", "README.md", "PKG-INFO")}
        with tarfile.open(source) as archive:
            files = [m.name for m in archive.getmembers() if m.isfile()]
            assert len(files) == len(set(files)) and set(files) == expected, files
            assert all(m.isfile() or m.isdir() for m in archive.getmembers()), "Links are forbidden."
            metadata(archive.extractfile(f"{prefix}/PKG-INFO").read(), project)
            for name in MODULES:
                assert archive.extractfile(f"{prefix}/src/expose_api/{name}").read() == (ROOT / "src/expose_api" / name).read_bytes()
            for filename in ("pyproject.toml", "pyproject.toml.orig"):
                assert tomllib.loads(archive.extractfile(f"{prefix}/{filename}").read().decode()) == tomllib.loads((ROOT / "pyproject.toml").read_text())
            archive.extractall(work / "source", filter="data")
        rebuilt = work / "rebuilt"
        run("uv", "build", "--wheel", "--no-sources", "--out-dir", rebuilt, work / "source" / prefix)
        assert wheel_files(rebuilt / wheel.name, version, project) == original_wheel, "Source rebuild differs."
        shutil.copytree(ROOT / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "examples", work / "examples", ignore=shutil.ignore_patterns("__pycache__"))
        installed(wheel, work, "wheel-install", version)
        installed(source, work, "source-install", version)
        profiles = {}
        if args.backpack:
            requirements = work / "backpack-requirements.txt"
            run("uv", "export", "--frozen", "--extra", "backpack", "--no-dev", "--no-emit-project", "--no-hashes",
                "--output-file", requirements)
            for label, artifact in (("wheel", wheel), ("source", source)):
                profiles[label] = installed(artifact, work, f"{label}-backpack", version, requirements=requirements)
        receipts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, source)}
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for path in (wheel, source):
                shutil.copy2(path, args.output_dir / path.name)
        result = {"version": version, "sha256": receipts, "optional_profiles": profiles}
        if args.receipt_file:
            args.receipt_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
