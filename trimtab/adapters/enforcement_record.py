"""The only writer of E20's pass cache: run by hooks/session-mode.sh in a run session (ADR 0063 §2-§3).

    python3 -I -B -X pycache_prefix=<none> hooks/run_module.py <root> trimtab.adapters.enforcement_record clear <dir>
    python3 -I -B -X pycache_prefix=<none> hooks/run_module.py <root> trimtab.adapters.enforcement_record record <dir>

The hook runs `clear` synchronously (bounded at 4 s, its git reads at 2 s), starts a new epoch,
then starts `record` in the background, both with the operator's launch environment, never the
session's shell. `record` holds one lock for its whole run, notes the epoch, removes the
project's cached pass before it reads anything, fetches the default branch into a fresh
repository, runs the gate, and writes a pass only when every row is shown and the epoch is still
the one it started under (so a recorder an earlier session left running cannot write after a
later session's clear). Every other way out leaves no pass: an error, SIGTERM from the hook's
timeout (raised as an exit, so the cleanup runs), or a kill (the pass was already removed). A
one-line status beside the cache names why.

`clear` exits 1 when it fails, so the hook removes every pass itself; `record` exits 0 whatever
happens: a background step has no one to tell, and no pass is what guards refuse on.

Residuals (ADR 0063 Consequences): a session can run this module, or any code, itself, and write
the cache through a path guard-paths cannot read from the command text; and the project's
repository is taken from its `origin` as configured, which the session can point at another
project the instance names. The server-side E22 job and the approver re-check every server fact,
so neither gains anything at merge.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from trimtab import guardstate, instance, roots
from trimtab.enforcement import gate, tools

REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
STATUS_SUFFIX = ".status"
UNKNOWN = "unknown-repository"
LOCK = ".lock"
EPOCH = ".epoch"
PASSED = "passed"
CLEAR_GIT_TIMEOUT_SECONDS = 2
CLEAR_LOCK_SECONDS = 1.0


def repository_of(project: Path, git: tools.Git) -> str:
    # The URL as configured, not as `insteadOf` rewrites it. It only names which project this is; the
    # gate then requires the instance file to name it, and fetches from github.com itself.
    url = git.config_get(project, "remote.origin.url")
    m = REMOTE.match(url)
    if not m or ".." in m.group(1):
        raise gate.GateError("origin is not a github.com repository")
    return m.group(1)


@contextmanager
def _locked(config: Path, wait: float | None = None):
    """The cache lock: blocking, or for at most `wait` seconds, after which the body runs unlocked (a clear
    may: the epoch stops any recorder it races from writing)."""
    directory = guardstate.directory(config, *gate.CACHE_PARTS, create=True)
    fd = os.open(directory / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)  # two recorders never interleave a clear and a write
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as err:
                    if err.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > deadline:
                        break
                    time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _epoch(config: Path) -> str | None:
    try:
        directory = guardstate.directory(config, *gate.CACHE_PARTS, create=False)
        return guardstate.read(directory / EPOCH)
    except guardstate.GuardStateError:
        return None


def _status(config: Path, repository: str | None, line: str) -> None:
    directory = guardstate.directory(config, *gate.CACHE_PARTS, create=True)
    name = gate.cache_name(repository) if repository else UNKNOWN
    guardstate.write(directory / (name + STATUS_SUFFIX), line[:4000] + "\n")


def clear(project: Path) -> None:
    """Remove this project's cached pass, or every cached pass when the project cannot be named."""
    config = guardstate.config_dir()
    try:
        repository = repository_of(project, tools.Git(timeout=CLEAR_GIT_TIMEOUT_SECONDS))
    except (gate.GateError, tools.ToolError):
        repository = None
    with _locked(config, wait=CLEAR_LOCK_SECONDS):
        gate.clear_pass(config, repository)


def record(project: Path) -> str:
    """Run the gate for `project`; cache a pass only if every row is shown. Returns the status line."""
    config = guardstate.config_dir()
    repository = None
    line = "not passed: the recorder did not finish"
    try:
        with _locked(config):
            epoch = _epoch(config)
            scratch = None
            try:
                git = tools.Git()
                repository = repository_of(project, git)
                gate.clear_pass(config, repository)  # before any read: an interrupted run leaves no pass
                scratch = tools.private_tempdir()
                facts = gate.gather(repository, project, roots.instance_root(), roots.code_root(), config,
                                    git=git, fetch_into=scratch / "head.git")
                failing = [f"{r.id}: {r.problem}" for r in gate.decide(facts) if r.problem]
                if failing:
                    line = "not passed: " + " | ".join(failing)
                elif _epoch(config) != epoch:
                    line = "not passed: a later session started a new epoch while this ran"
                else:
                    gate.record_pass(config, repository, facts.pinned)
                    line = PASSED
            except (gate.GateError, tools.ToolError, roots.InstanceError, instance.InstanceFileError) as err:
                line = f"not passed: {err}"
            finally:
                if scratch is not None:
                    tools.remove_tree(scratch)
                if line != PASSED:
                    gate.clear_pass(config, repository)  # every pass when the repository is unknown
    finally:
        try:
            _status(config, repository, line)
        except (guardstate.GuardStateError, OSError):
            pass  # the status is the operator's note; the pass, the fact guards read, is already settled
    return line


def _terminate(signum, frame):
    raise SystemExit(128 + signum)  # unwinds through record's finally, which removes any pass


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("clear", "record"):
        return 0
    signal.signal(signal.SIGTERM, _terminate)
    try:
        (clear if argv[0] == "clear" else record)(Path(argv[1]))
    except Exception:  # noqa: BLE001 - reported by exit code only: the hook has no channel, and no pass is left
        # A failed clear exits 1, so the hook removes every pass itself; a failed record has already removed its own.
        return 1 if argv[0] == "clear" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
