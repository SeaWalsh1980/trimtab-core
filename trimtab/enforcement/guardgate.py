"""E21: what a guard asks before it allows anything in a run session (ADR 0063 §3).

`holds(project)` answers whether the enforcement cache holds a pass for the current key, and, where it
does not, starts the recorder once so a merge to the default branch mid-run costs a short wait rather
than a stop. The guards never read the pass file themselves; they call this.

Standard library only: the hooks run Python with `-I -S` (ENG-3). Everything fails closed: a read that
fails, a key that cannot be built, or a pass under another key is no pass.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from trimtab import guardstate, planfacts, roots
from trimtab.enforcement import gate, tools

MODULE = "trimtab.adapters.enforcement_record"
START_LOCK = "recorder.start"
RUNNER = "run_module.py"


def repository_of(project: Path, environ: Mapping[str, str] = os.environ) -> str | None:
    """The project's repository, from its `origin` as configured. None where it names none."""
    try:
        git = tools.Git(environ=environ)
        return planfacts.repository_of(git.config_get(project, "remote.origin.url"))
    except Exception:  # noqa: BLE001 - every failure is "no repository", which is no pass
        return None


def holds(project: Path, environ: Mapping[str, str] = os.environ, *, start: bool = True) -> bool:
    """Whether the gate's pass holds for this session's key; otherwise start the recorder and say no."""
    config = guardstate.config_dir()
    repository = repository_of(project, environ)
    if repository is None:
        return False
    try:
        instance_root = roots.instance_root(environ=environ)
        code = roots.code_root()
    except roots.InstanceError:
        return False
    if gate.pass_holds(repository, project, instance_root, code, config, tools.Git(environ=environ)):
        return True
    if start:
        start_recorder(project, environ)
    return False


def start_recorder(project: Path, environ: Mapping[str, str] = os.environ) -> bool:
    """Start one recorder in the background, or nothing where one is already running (ADR 0063 §3).

    The lock below is the start lock, not the recorder's own: the recorder holds its own lock for its
    whole run, so two that did start would serialise rather than race. This only keeps a guarded call
    from starting a second one while the first is still working."""
    try:
        config = guardstate.config_dir()
        directory = guardstate.directory(config, *gate.CACHE_PARTS, create=True)
        path = directory / START_LOCK
        handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except (guardstate.GuardStateError, OSError):
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # a recorder a guard started is still running
        runner = roots.code_root() / "hooks" / RUNNER
        if not runner.is_file():
            return False
        argv = [tools.trusted("python3"), "-I", "-S", "-B", "-X", "pycache_prefix=/dev/null/trimtab",
                str(runner), str(roots.code_root()), MODULE, str(project)]
        with open(os.devnull, "wb") as quiet:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=quiet, stderr=quiet,
                             start_new_session=True, cwd=str(project), env=dict(environ))
        return True
    except (OSError, tools.ToolError, ValueError):
        return False
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def main(argv: list[str]) -> int:
    """`python3 -m` entry for a guard that reports the gate's state: 0 when a pass holds, 1 when not."""
    project = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    return 0 if holds(project) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
