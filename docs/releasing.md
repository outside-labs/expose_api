# Publishing a release

The initial candidate is `expose-api` 0.1.0, imported as `expose_api`. Preparation
does not publish a tag, a GitHub release or a package.

1. Merge the implementation PRs in dependency order with passing checks and verify
   final main. Keep parent branches while open dependent PRs target them.
2. Run `uv run --frozen python scripts/check_distribution.py --tag v0.1.0 --output-dir /path/to/new/candidate-directory`.
   Review exact contents, metadata, tests and SHA256 receipts. Version, tag and
   package `__version__` must agree.
3. Confirm the PyPI Trusted Publisher identifies owner `outside-labs`, repository
   `expose_api`, workflow `release.yaml`, and environment `pypi`. The publisher is
   configured separately on PyPI; this repository does not inspect that account.
4. Review/create the GitHub `pypi` environment and its release access policy before
   publication. Release preparation leaves environment security policies unchanged.
5. When publication is authorized, create an immutable `v0.1.0` tag on the verified
   main commit and push it. This **starts the upload workflow**.
6. `.github/workflows/release.yaml` reruns checks, verifies tag/version agreement,
   builds and installs distributions, and passes the exact verified artifact to an
   isolated publish job. Only that job has `id-token: write` and uses `pypi`.
7. Confirm workflow success, compare PyPI file hashes with workflow artifact
   receipts, and verify a fresh installation from the registry. A GitHub release
   can then reference the immutable tag and those same artifacts.

Never replace a released version/tag, put an API token into a workflow, or upload
files rebuilt after verification. License terms remain unspecified; a public
repository is not a license grant.
