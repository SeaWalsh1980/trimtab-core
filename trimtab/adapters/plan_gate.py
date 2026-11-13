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

from trimtab import planacts, planguard, planledger, planrows
from trimtab.enforcement import guardgate

ALLOW, BLOCK = 0, 2
DEADLINE = 8  # seconds, inside the hook's own 10: the ls-remote alone is bounded at 3 (ADR 0063 §3)
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# The one command a run may make while the gate fails: `trimtab check-enforcement`, as the whole
# command. Its own options are allowed; anything else in the command text is not.
REPORT = re.compile(r"(?:\S*/)?(?:bin/)?trimtab$")
REPORT_SUB = "check-enforcement"
MESSAGE = ("E21: the enforcement gate has not passed for this session's lock, base and default-branch "
           "head, so a plan run writes nothing (ADR 0063 §3, AGW-4). A recorder has been started; run "
           "`trimtab check-enforcement` to see which row is not shown. Do not look for another route — "
           "only the operator lifts this, by exporting HOOK_ALLOW_PATHS=1 before launching claude.")


def reports_only(command: str) -> bool:
    """Whether a Bash command is `trimtab check-enforcement` and nothing else."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 2 or not REPORT.fullmatch(words[0]) or words[1] != REPORT_SUB:
        return False
    # Only this subcommand's own options may follow, never another command: a separator or a word that
    # could start one makes it more than a report.
    return all(w.startswith("-") or "=" in w or not re.search(r"[;&|`$<>()]", w) for w in words[2:]) and \
        not re.search(r"[;&|`$<>()\n]", command)


def decide(payload: dict, holds=guardgate.holds, resume=planacts.resume_ok) -> tuple[int, str]:
    """(exit code, message) for one call in a run session. The caller decides the session is a run."""
    tool = payload.get("tool_name")
    command = (payload.get("tool_input") or {}).get("command")
    if tool not in (*WRITE_TOOLS, "Bash"):
        return ALLOW, ""
    if tool == "Bash":
        if not isinstance(command, str):
            return BLOCK, "guard-paths: blocked: a Bash command that is not a string, in a plan run"
        if reports_only(command):
            return ALLOW, ""
    cwd = payload.get("cwd")
    where = Path(cwd) if isinstance(cwd, str) and cwd.startswith("/") else None
    if where is None:
        return BLOCK, "guard-paths: blocked: a call in a plan run whose checkout cannot be read"
    if not holds(where):
        return BLOCK, f"BLOCKED by guard-paths: {MESSAGE}"
    return _resume(where, resume)


def _resume(where: Path, resume) -> tuple[int, str]:
    """E13: a write on a `plan/*` branch needs the resume check passed for the branch's head
    (ADR 0063 §5). The branch comes from the checkout, so a call in another repository reads nothing."""
    support = planguard.PlanSupport(where)
    try:
        branch = support.head_branch(where)
    except Exception as err:  # noqa: BLE001 - a checkout this guard cannot read is a refusal
        return BLOCK, f"BLOCKED by guard-paths: the checkout's branch cannot be read ({type(err).__name__})"
    if branch is None or not planrows.plan_branch(branch):
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
