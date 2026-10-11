# 0015. CI installs PyYAML by hash, from requirements the workflow writes

## Status

Accepted. Refines the PyYAML step of ADR 0014, which stays as written.

## Context

ADR 0014 had CI install `pyyaml==6.0.3` with `--only-binary`, so the exact
version was pinned but the file was not: PyPI serving a different wheel under
that version would have been installed. The review of the pull request that
added the step noted the missing `--require-hashes`.

The step runs before the base-expectation steps, which are ordered so that
nothing the pull request wrote can act on the base guard matrix. An installed
package can run code at every interpreter start (a `.pth` file), so whatever
the step installs runs in every later step, the base expectations included.

## Decision

- **Install with `--require-hashes`**, listing the sha256 of the PyYAML 6.0.3
  wheels for Python 3.11 on manylinux x86_64 and aarch64. pip refuses any file
  whose hash is not listed.
- **Write the requirements from the workflow**, with a heredoc into the
  runner's temp directory, and install from that file. The hashes live in
  `ci.yml`, not in a file in the tree.

## Options considered

- **A requirements file in the tree** (for example `.github/requirements-ci.txt`).
  Rejected. It is a file the pull request can edit, so a PR could name another
  package or hash and have its wheel installed, and run through a `.pth` file,
  before the base expectations. That is the exposure the step's placement was
  argued not to have. Editing `ci.yml` itself is already a reviewed control
  change; editing a data file is not.
- **A lock file produced by `pip-compile`.** Rejected for the same reason, and
  for a tool dependency to maintain one package.
- **Keep the version pin alone.** Rejected. It trusts PyPI to serve the same
  bytes for a version, which is the gap this closes.
- **Hashes for every wheel of the release.** Rejected. The runner is x86_64
  and the aarch64 hash covers the arm runner image; the musl, s390x and other
  hashes would be unused.

## Consequences

- A PyYAML file that differs from the listed hashes fails the step, and so the
  job, before any check runs.
- Accepted: moving PyYAML, or running CI on another platform or Python minor
  version, means editing the hashes in `ci.yml` by hand. Dependabot does not
  track them.
- Trial, kept as a result: a requirements file with two hashes altered fails
  `pip download --require-hashes` with the expected and got values, and the
  unaltered file downloads the 3.11 x86_64 wheel.
