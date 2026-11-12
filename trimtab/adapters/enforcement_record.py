"""The only writer of E20's pass cache: run by hooks/session-mode.sh in a run session (ADR 0063 §2-§3).

    python3 -I -B -X pycache_prefix=<none> hooks/run_module.py <root> trimtab.adapters.enforcement_record clear <dir>
    python3 -I -B -X pycache_prefix=<none> hooks/run_module.py <root> trimtab.adapters.enforcement_record record <dir> [<epoch>]

The hook starts a new epoch, runs `clear` synchronously (bounded at 4 s; it removes every pass and
reads no git), then starts `record` in the background with that epoch, both with the operator's
launch environment, never the session's shell. `record` holds one lock for its whole run, removes
the project's cached pass before it reads anything, fetches the default branch into a fresh
repository, runs the gate, and writes a pass only when every row is shown and the epoch it was
handed is still current, checked before and after the write (so a recorder an earlier session
left running, or still waiting on the lock, cannot write after a later session's clear). With no
epoch, or one it cannot read, it writes no pass. Every other way out leaves no pass: an error,
SIGTERM from the hook's timeout (raised as an exit, so the cleanup runs), or a kill (the pass was
already removed). A one-line status beside the cache names why.

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
EPOCH = gate.EPOCH
PASSED = "passed"
CLEAR_LOCK_SECONDS = 1.0
EPOCH_SHAPE = gate.EPOCH_SHAPE


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


_epoch = gate.current_epoch


def _status(config: Path, repository: str | None, line: str) -> None:
    directory = guardstate.directory(config, *gate.CACHE_PARTS, create=True)
    name = gate.cache_name(repository) if repository else UNKNOWN
    guardstate.write(directory / (name + STATUS_SUFFIX), line[:4000] + "\n")


def clear(project: Path) -> None:
    """Remove every cached pass. A session start clears them all, not by the project's `origin`, which the
    session could point elsewhere to spare its own project's pass. It reads no git, so nothing can stall it."""
    del project  # named for the hook's call; deliberately not read
    config = guardstate.config_dir()
    with _locked(config, wait=CLEAR_LOCK_SECONDS):
        gate.clear_pass(config, None)
        # The status lines go too: one left saying "passed" would contradict an empty cache.
        for status in guardstate.directory(config, *gate.CACHE_PARTS, create=False).glob("*" + STATUS_SUFFIX):
            os.unlink(status)


def record(project: Path, epoch: str | None = None) -> str:
    """Run the gate for `project`; cache a pass only if every row is shown and `epoch` (the one the hook handed
    over, else the current one) is still current, before and after the write. Returns the status line."""
    config = guardstate.config_dir()
    repository = None
    line = "not passed: the recorder did not finish"
    expected = epoch if epoch is None or EPOCH_SHAPE.fullmatch(epoch) else ""
    if expected is None:
        # A recorder a guard starts mid-session (E21) takes the epoch current when it starts, before it waits
        # on the lock: a later session that moves the epoch meanwhile then stops it from writing.
        expected = _epoch(config) or ""
    if not expected:
        # No epoch to write under: nothing it read could become a pass, so it reads nothing.
        line = "not passed: there is no epoch to write a pass under"
        try:
            _status(config, None, line)
        except (guardstate.GuardStateError, OSError):
            pass  # the status is the operator's note; no pass was or will be written
        return line
    try:
        with _locked(config):
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
                elif not expected:
                    line = "not passed: there is no epoch to write a pass under"
                elif _epoch(config) != expected:
                    line = "not passed: a later session started a new epoch while this ran"
                else:
                    # The pass carries its epoch, so a guard refuses it the moment the epoch moves.
                    gate.record_pass(config, repository, facts.pinned, expected)
                    # Checked again after the write: an epoch that moved during it voids the pass.
                    line = PASSED if _epoch(config) == expected else "not passed: the epoch moved as the pass was written"
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
    shapes = {"clear": (2,), "record": (2, 3)}
    if not argv or argv[0] not in shapes or len(argv) not in shapes[argv[0]]:
        # A clear that did not run must not read as one that did: the hook then removes every pass itself.
        return 1 if argv and argv[0] == "clear" else 0
    signal.signal(signal.SIGTERM, _terminate)
    try:
        if argv[0] == "clear":
            clear(Path(argv[1]))
        else:
            record(Path(argv[1]), argv[2] if len(argv) == 3 else None)
    except Exception:  # noqa: BLE001 - reported by exit code only: the hook has no channel, and no pass is left
        # A failed clear exits 1, so the hook removes every pass itself; a failed record has already removed its own.
        return 1 if argv[0] == "clear" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
