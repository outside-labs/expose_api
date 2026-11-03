# Portable acceptance runners

Automatic checks qualify Python 3.14 on Linux, Windows and the explicit standard
`macos-15-intel` runner. All existing model, HTTP, storage, event and artifact
checks remain selected. Installed event receipts record the actual operating
system and machine architecture.

The Checks workflow can also be dispatched manually with `macos_runner` set to
`macos-latest` for ARM64 qualification. That selection uses the same tests and
artifact checks; it does not select a larger runner or another service. The
explicit automatic default avoids the ARM64 capacity failures observed during
the optional validation work. GitHub's [runner inventory](https://github.com/actions/runner-images/blob/main/README.md)
records the available standard labels. Runner availability and finite platform
evidence do not change runtime dependencies or package publication gates.
