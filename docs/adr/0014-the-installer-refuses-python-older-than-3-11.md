# 0014. The installer refuses a python3 older than 3.11

## Status

Accepted.

## Context

The launchers (`bin/trimtab`, the drift hook and `pr-body-check.sh`) run
`python3 -P`, and `-P` exists from Python 3.11. On an older interpreter
`bin/trimtab` fails loudly, the drift hook goes quiet, and `pr-body-check.sh`
fails open through its probe. Nothing declared the floor or enforced it: there
was no `pyproject.toml` or `.python-version`, CI ran whatever `python3` the
runner image shipped, and the only 3.11 check was in `trimtab/routines.py`,
when templates are rendered. The base is public and meant for other operators,
and they are the ones an old interpreter would bite.

The constraints:

- The check must fire before the failure it prevents, which is after install.
- `shim/bootstrap.sh` is copied byte for byte into every instance at the pin,
  so changing it forces a change in each instance.
- The probe has to run on the interpreters it refuses, so it cannot use `-P`.
- CI has a single required job, `lint-test`, and an ordering inside it that
  keeps the base branch's guard expectations out of reach of anything a pull
  request wrote.

## Decision

- **`bootstrap.sh` (the installer, not the shim) refuses a `python3` older
  than 3.11**, with exit 1 and one line naming the version found. The probe
  sits after argument parsing and before the first step that reads the
  instance, writes, links or switches, so `--check` is refused too. It tests
  `sys.version_info >= (3, 11)`.
- **`ci.yml` installs Python 3.11** in the `lint-test` job, with
  `actions/setup-python` pinned by full commit SHA (the pin style of
  `actions/checkout`, kept current by Dependabot, ADR 0009), so CI runs the
  floor and not the image's default.
- **The same job installs PyYAML 6.0.3 from a wheel** right after it. The
  toolcache interpreter carries no PyYAML, which the installer and the
  registry modules import.

Both CI steps sit directly after checkout and before the base-expectation
steps. They run pinned code that reads nothing the pull request wrote (an
explicit version, no version file, no cache key from the tree; one exact wheel,
`--only-binary`, so no build script runs; run with `-I`, so the checkout is not on `sys.path`). They change only which `python3`
the later steps find, so the base guard matrix is still materialised, and the
python3-fallback run still starts, before anything in the pull request can act
on them. Those runs now use the floor as well.

## Options considered

- **`requires-python` in a `pyproject.toml`, alone.** Rejected. Nothing
  installs the base as a package, so nothing would read it.
- **A probe in each launcher.** Rejected. It duplicates the check three times
  and fires too late, after install, when the failure has already happened.
- **A probe in `shim/bootstrap.sh`.** Rejected. The shim is byte-copied into
  every instance, so changing it forces an instance-side re-copy, for a check
  the installer can make on its own.
- **A new CI job for the floor.** Rejected. The ruleset requires `lint-test`
  by name, and a second job was already found to bill a minute for seconds of
  work.
- **PyYAML floated (`pip install pyyaml`).** Rejected. A moving version
  installed on every run is an unreviewed dependency change.
- **PyYAML from the runner's apt `python3-yaml` through `PYTHONPATH`.**
  Rejected. It couples the 3.11 interpreter to the dist-packages directory of
  a different minor version.
- **setup-python placed after the base-expectation steps.** Rejected. Those
  steps, and the python3-fallback run in particular, would then test on the
  image's default and not on the floor.

## Consequences

- An operator on an old interpreter is refused at install, with the version
  named, not at the first launcher run.
- The refusal is a single line before any write, so a refused run leaves no
  trace to clean up.
- CI exercises 3.11 only. A newer interpreter is not run there; the floor is
  the version that has to work.
- Accepted: the PyYAML pin does not move on its own. Dependabot's
  `github-actions` ecosystem does not track it, so it is bumped by hand.
- Accepted: the version probe is one `python3` call that costs a few
  milliseconds on every install and `--check`.
