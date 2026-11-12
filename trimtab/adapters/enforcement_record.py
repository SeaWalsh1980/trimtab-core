"""The only writer of E20's pass cache: run by hooks/session-mode.sh in a run session (ADR 0063 §2-§3).

    python3 -I -B hooks/run_module.py <root> trimtab.adapters.enforcement_record <project dir>

The hook starts it in the background, with the operator's launch environment, never the
session's shell. It reads the project's repository from its `origin` remote (which must be a
project the instance file names, or no project row is shown), runs the gate, and then either
caches the pass or removes any cached one. It writes a one-line status beside the cache, naming
the rows not shown, so the operator can see why guards refuse. It prints nothing and exits 0:
a background step has no one to tell, and a failure leaves no pass, which guards read as refuse.

Residual (ADR 0063 Consequences): a session can run this module, or any code, itself. A run it
starts with a forged environment or a modified copy can still write the cache, because guard-paths
sees only command text. The server-side E22 job and the approver re-check every server fact, so
such a pass gains nothing at merge.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from trimtab import guardstate, roots
from trimtab.enforcement import gate, tools

REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
STATUS_SUFFIX = ".status"


def repository_of(project: Path, git: tools.Git) -> str:
    # The URL as configured, not as `insteadOf` rewrites it; which project this is comes only from here,
    # and the gate then requires the instance file to name it.
    url = git(project, "config", "--get", "remote.origin.url").strip()
    m = REMOTE.match(url)
    if not m or ".." in m.group(1):
        raise gate.GateError("origin is not a github.com repository")
    return m.group(1)


def _status(config: Path, repository: str, line: str) -> None:
    directory = guardstate.directory(config, *gate.CACHE_PARTS, create=True)
    guardstate.write(directory / (gate.cache_name(repository) + STATUS_SUFFIX), line[:4000] + "\n")


def record(project: Path) -> str:
    """Run the gate for `project`; cache a pass or clear it. Returns the status line written."""
    config = guardstate.config_dir()
    git = tools.Git()
    repository = repository_of(project, git)
    try:
        facts = gate.gather(repository, project, roots.instance_root(), roots.code_root(), config, git=git,
                            fetch=True)
        results = gate.decide(facts)
    except (gate.GateError, roots.InstanceError) as err:
        gate.clear_pass(config, repository)
        line = f"not passed: {err}"
    except BaseException:
        gate.clear_pass(config, repository)  # an unexpected failure must not leave an earlier pass standing
        raise
    else:
        failing = [f"{r.id}: {r.problem}" for r in results if r.problem]
        if failing:
            gate.clear_pass(config, repository)
            line = "not passed: " + " | ".join(failing)
        else:
            gate.record_pass(config, repository, facts.pinned)
            line = "passed"
    _status(config, repository, line)
    return line


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        return 0
    try:
        record(Path(argv[0]))
    except Exception:  # noqa: BLE001 - a background step: any failure leaves no pass, which guards refuse on
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
