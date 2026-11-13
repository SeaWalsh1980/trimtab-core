"""E21 for guard-paths: in a run session, nothing is written until the gate has passed (ADR 0063 §3).

Unless the enforcement cache holds a pass for the current key, guard-paths refuses every Edit, Write,
MultiEdit and NotebookEdit, and every Bash command except `trimtab check-enforcement` as the whole
command — a shell can write any file, so the only command left while the gate fails is the one that
reports on it. A guard that finds no pass starts the recorder (at most one), so a merge to the default
branch mid-run costs a short wait rather than a stop.

guard-github has the other half (every GitHub write and every `git push`), in its own adapter.

Standard library only: the hook runs Python with `-I -S` (ENG-3). Fails closed (ADR 0001): a payload
it cannot read in a run session blocks.
"""

from __future__ import annotations

import json
import re
import shlex
import signal
import sys
from pathlib import Path

from trimtab import planacts, planguard, planledger, planrows, roots
from trimtab.enforcement import guardgate

ALLOW, BLOCK = 0, 2
DEADLINE = 8  # seconds, inside the hook's own 10: the ls-remote alone is bounded at 3 (ADR 0063 §3)
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# A Skill call writes no file, but a review round recorded while the gate fails would count for E10,
# E11 and E23, so a run needs the pass for one too: tighter than the row's own list (Amendment 3).
GATED_TOOLS = (*WRITE_TOOLS, "Bash", "Skill")
# The one command a run may make while the gate fails: `trimtab check-enforcement`, as the whole
# command. Its own options are allowed; anything else in the command text is not. The first word must
# name the installed `bin/trimtab` by a path that resolves to it — a bare name would take whatever the
# session's PATH finds, and a basename match would run any file a run called `trimtab`.
REPORT_SUB = "check-enforcement"
MESSAGE = ("E21: the enforcement gate has not passed for this session's lock, base and default-branch "
           "head, so a plan run writes nothing (ADR 0063 §3, AGW-4). The only command left is the "
           "installed bin/trimtab named by its path, with `check-enforcement`, which says which row is "
           "not shown. Do not look for another route — only the operator lifts this, by exporting "
           "HOOK_ALLOW_PATHS=1 before launching claude.")


def installed_report(where: Path) -> Path | None:
    """The `bin/trimtab` of the pinned base this checkout's guards run from, resolved, or None."""
    try:
        found = (roots.code_root() / "bin" / "trimtab").resolve()
    except (roots.InstanceError, OSError):
        return None
    return found if found.is_file() else None


def reports_only(command: str, where: Path | None = None) -> bool:
    """Whether a Bash command is the installed `trimtab check-enforcement` and nothing else."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 2 or words[1] != REPORT_SUB:
        return False
    installed = installed_report(where or Path.cwd())
    if installed is None:
        return False  # nothing to compare the command against: no command is the report (fail closed)
    try:
        named = Path(words[0]) if Path(words[0]).is_absolute() else (where or Path.cwd()) / words[0]
        if named.resolve() != installed:
            return False
    except OSError:
        return False
    # Only this subcommand's own options may follow, never another command: a separator or a word that
    # could start one makes it more than a report.
    return all(w.startswith("-") or "=" in w or not re.search(r"[;&|`$<>()]", w) for w in words[2:]) and \
        not re.search(r"[;&|`$<>()\n]", command)


def decide(payload: dict, holds=None, resume=planacts.resume_ok) -> tuple[int, str]:
    """(exit code, message) for one call in a run session. The caller decides the session is a run.

    `holds` is resolved at the call, so both guards' halves of E21 rest on the one decision function the
    enforcement manifest names (`guardgate.holds`)."""
    holds = holds or guardgate.holds
    tool = payload.get("tool_name")
    command = (payload.get("tool_input") or {}).get("command")
    if tool not in GATED_TOOLS:
        return ALLOW, ""
    if tool == "Bash":
        if not isinstance(command, str):
            return BLOCK, "guard-paths: blocked: a Bash command that is not a string, in a plan run"
        here = payload.get("cwd")
        if reports_only(command, Path(here) if isinstance(here, str) and here.startswith("/") else None):
            return ALLOW, ""
    cwd = payload.get("cwd")
    where = Path(cwd) if isinstance(cwd, str) and cwd.startswith("/") else None
    if where is None:
        return BLOCK, "guard-paths: blocked: a call in a plan run whose checkout cannot be read"
    if not holds(where):
        return BLOCK, f"BLOCKED by guard-paths: {MESSAGE}"
    if tool == "Skill":
        return ALLOW, ""  # it writes no file, so E13's resume check has nothing to judge for it
    return _resume(where, resume)


def _resume(where: Path, resume) -> tuple[int, str]:
    """E13: a write on a `plan/*` branch needs the resume check passed for the branch's head
    (ADR 0063 §5). The branch comes from the checkout, so a call in another repository reads nothing."""
    support = planguard.PlanSupport(where)
    branch = support.head_branch(where)
    if branch is None:
        # `head_branch` answers None both for "no repository here" and for a git that failed or a
        # detached HEAD, and the second must not read as "no plan branch, nothing to judge": in a run
        # a checkout whose branch cannot be read is a refusal (SEC-2.5).
        if not support.in_repository(where):
            return ALLOW, ""
        return BLOCK, ("BLOCKED by guard-paths: the checkout's branch cannot be read, so E13 cannot be "
                       "judged; a plan run writes nothing until it can (ADR 0063 §5)")
    if not planrows.plan_branch(branch):
        return ALLOW, ""
    try:
        resume(support, branch)
    except planrows.Refusal as why:
        return BLOCK, (f"BLOCKED by guard-paths: {why} The plan-run layer makes no write on a branch "
                       "whose resume check has not passed for its head (ADR 0063 §5).")
    except (planrows.FactsError, planledger.LedgerError) as why:
        return BLOCK, (f"BLOCKED by guard-paths: the branch's resume check could not be read ({why}); "
                       "refusing (ADR 0063 §5)")
    return ALLOW, ""


def _out_of_time(signum, frame):
    raise TimeoutError("the deadline passed")


def main() -> int:
    signal.signal(signal.SIGALRM, _out_of_time)
    signal.alarm(DEADLINE)
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        code, message = decide(payload)
    except Exception as err:  # fail closed: a gate check that cannot run blocks the call (ADR 0001)
        print(f"BLOCKED by guard-paths: the enforcement gate could not be read ({type(err).__name__}); "
              "a plan run writes nothing until it passes (ADR 0063 §3)", file=sys.stderr)
        return BLOCK
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
