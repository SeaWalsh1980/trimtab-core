"""The only writer of E20's pass cache: run by hooks/session-mode.sh in a run session (ADR 0063 §2-§3).

    python3 -I -B hooks/run_module.py <root> trimtab.adapters.enforcement_record clear <project dir>
    python3 -I -B hooks/run_module.py <root> trimtab.adapters.enforcement_record record <project dir>

The hook runs `clear` synchronously, then starts `record` in the background, both with the
operator's launch environment, never the session's shell. `record` holds one lock for its whole
run, removes the project's cached pass before it reads anything, fetches the default branch into a
fresh repository, runs the gate, and writes a pass only when every row is shown. Every other way
out, an error, SIGTERM from the hook's timeout, or a kill (the pass was already removed), leaves
no pass. A one-line status beside the cache names the rows not shown. Both subcommands print
nothing and exit 0: a background step has no one to tell, and no pass is what guards refuse on.

Residual (ADR 0063 Consequences): a session can run this module, or any code, itself, and write
the cache through a path guard-paths cannot read from the command text. The server-side E22 job
and the approver re-check every server fact, so such a pass gains nothing at merge.
"""

from __future__ import annotations

import fcntl
import os
import re
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

from trimtab import guardstate, instance, roots
from trimtab.enforcement import gate, tools

REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
STATUS_SUFFIX = ".status"
UNKNOWN = "unknown-repository"
LOCK = ".lock"
PASSED = "passed"


def repository_of(project: Path, git: tools.Git) -> str:
    # The URL as configured, not as `insteadOf` rewrites it. It only names which project this is;
    # the gate then requires the instance file to name it, and fetches from github.com itself.
    url = git(project, "config", "--get", "remote.origin.url").strip()
    m = REMOTE.match(url)
    if not m or ".." in m.group(1):
        raise gate.GateError("origin is not a github.com repository")
    return m.group(1)


@contextmanager
def _locked(config: Path):
    directory = guardstate.directory(config, *gate.CACHE_PARTS, create=True)
    fd = os.open(directory / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # two recorders never interleave a clear and a write
        yield
    finally:
        os.close(fd)


def _status(config: Path, repository: str | None, line: str) -> None:
    directory = guardstate.directory(config, *gate.CACHE_PARTS, create=True)
    name = gate.cache_name(repository) if repository else UNKNOWN
    guardstate.write(directory / (name + STATUS_SUFFIX), line[:4000] + "\n")


def clear(project: Path) -> None:
    """Remove this project's cached pass, or every cached pass when the project cannot be named."""
    config = guardstate.config_dir()
    try:
        repository = repository_of(project, tools.Git())
    except (gate.GateError, tools.ToolError):
        repository = None
    gate.clear_pass(config, repository)


def record(project: Path) -> str:
    """Run the gate for `project`; cache a pass only if every row is shown. Returns the status line."""
    config = guardstate.config_dir()
    with _locked(config):
        repository = None
        line = "not passed: the recorder did not finish"
        scratch = None
        try:
            git = tools.Git()
            repository = repository_of(project, git)
            gate.clear_pass(config, repository)  # before any read: an interrupted run leaves no pass
            scratch = tools.private_tempdir()
            facts = gate.gather(repository, project, roots.instance_root(), roots.code_root(), config, git=git,
                                fetch_into=scratch / "head.git")
            failing = [f"{r.id}: {r.problem}" for r in gate.decide(facts) if r.problem]
            if failing:
                line = "not passed: " + " | ".join(failing)
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
            _status(config, repository, line)
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
