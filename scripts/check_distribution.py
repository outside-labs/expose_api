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
MODULES = {"__init__.py", "asgi.py", "errors.py", "models.py", "py.typed", "resources.py", "server.py", "stores.py"}


def run(*args, cwd=ROOT):
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def metadata(raw, project):
    message = email.parser.BytesParser().parsebytes(raw)
    for key, expected in (("Name", "expose-api"), ("Version", project["version"]),
                          ("Requires-Python", ">=3.14"), ("Summary", project["description"]),
                          ("Description-Content-Type", "text/markdown")):
        assert message.get_all(key) == [expected], (key, message.get_all(key))
    assert not message.get_all("Requires-Dist"), "Runtime dependencies are forbidden."
    assert message.get_payload().strip() == (ROOT / "README.md").read_text().strip()


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


def installed(artifact, work, name, version):
    environment = work / name
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run("uv", "pip", "install", "--python", python, "--no-deps", artifact, cwd=work)
    probe = (
        "import importlib.metadata as m, pathlib, sys, expose_api; "
        f"assert m.version('expose-api') == expose_api.__version__ == {version!r}; "
        "assert not m.requires('expose-api'); "
        "assert pathlib.Path(expose_api.__file__).is_relative_to(pathlib.Path(sys.prefix)); "
        "assert pathlib.Path(expose_api.__file__).with_name('py.typed').is_file(); "
        "assert not any(k in sys.modules for k in ('starlette','fastapi','pydantic','hidden_moves','backpack'))"
    )
    run(python, "-I", "-c", probe, cwd=work)
    run(python, "-I", "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", work / "tests", "-v", cwd=work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--output-dir", type=Path)
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
        receipts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, source)}
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for path in (wheel, source):
                shutil.copy2(path, args.output_dir / path.name)
        print(json.dumps({"version": version, "sha256": receipts}, indent=2))


if __name__ == "__main__":
    main()
