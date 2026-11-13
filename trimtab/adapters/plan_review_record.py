"""E11's review recorder and E23's sixth-round refusal, a branch of guard-paths (ADR 0063 §5).

A PostToolUse hook on the Skill tool appends a `round` entry to the part's ledger when
`trimtab-review`, `security-review` or `code-review` completes, naming the checkout's `HEAD` at the
call. A PreToolUse one refuses a sixth pre-draft round at its first pass. A pass run any other way —
a slash command the operator types, a pass run by hand — records nothing, so it counts for nothing.

This lives in guard-paths rather than a hook of its own because the manifest's "Guard" owner names
only `guard-paths.sh` and `guard-github.sh` (ADR 0063 §2), so a new hook file could never show its
row. Registering it for the Skill tool is the operator's control PR.

Standard library only: the hook runs Python with `-I -S` (ENG-3). Fails closed (ADR 0001): a call it
cannot read on a `plan/*` branch blocks.
"""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

from trimtab import planacts, planguard, planledger, planrows

ALLOW, BLOCK = 0, 2
DEADLINE = 6  # seconds, inside the hook's own 10 (ADR 0063 Amendment 1)
EVENTS = ("PreToolUse", "PostToolUse")


def pass_name(skill: object) -> str | None:
    """The review pass a Skill call runs, or None. A plugin prefix (`plugin:code-review`), a leading
    slash and arguments (`code-review ultra`) all name the same pass."""
    if not isinstance(skill, str) or not skill.strip():
        return None
    word = skill.strip().split()[0].lstrip("/")
    if ":" in word:
        word = word.rpartition(":")[2]
    return word if word in planledger.PASSES else None


def failed(payload: dict) -> bool:
    """Whether the call's own result says the pass did not complete.

    A pass that errored is no round: E11 exists to show that the pass actually ran. The harness does not
    promise a shape here, so only an answer that says error is read as one, and a call with no result is
    recorded as before — named as a residual in ADR 0063 Amendment 3."""
    answer = payload.get("tool_response")
    if isinstance(answer, dict):
        return bool(answer.get("is_error") or answer.get("isError") or answer.get("error"))
    return False


def decide(payload: dict, support=None) -> tuple[int, str]:
    """(exit code, message) for one Skill call. A call on no plan part reads nothing and allows."""
    if payload.get("tool_name") != "Skill" or payload.get("hook_event_name") not in EVENTS:
        return ALLOW, ""
    name = pass_name((payload.get("tool_input") or {}).get("skill"))
    if name is None:
        return ALLOW, ""
    if payload["hook_event_name"] == "PostToolUse" and failed(payload):
        return ALLOW, ""  # the pass did not complete, so it is no round
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return BLOCK, "guard-paths: blocked: a review pass whose checkout cannot be read (fail closed)"
    where = Path(cwd)
    found = support or planguard.PlanSupport(where)
    branch = found.head_branch(where)
    if branch is None:
        # No repository or a detached HEAD means no plan part to count a round for; a git that failed to
        # answer is the case that must not read as that (SEC-2.5).
        if found.branch_unreadable(where):
            return BLOCK, ("guard-paths: blocked: a review pass whose branch cannot be read cannot be "
                           "counted, so E11 and E23 could not hold (ADR 0063 §5)")
        return ALLOW, ""
    if not planrows.plan_branch(branch):
        return ALLOW, ""  # not a plan part: E11 counts nothing and reads nothing
    head = found.head_sha(where)
    if head is None:
        return BLOCK, ("guard-paths: blocked: a review pass on a plan/* branch whose HEAD cannot be read "
                       "cannot be recorded, so E11 could not count the round (ADR 0063 §5)")
    try:
        if payload["hook_event_name"] == "PreToolUse":
            planacts.round_starting(found, branch, head)
        else:
            planacts.round_recorded(found, branch, head, name)
    except planrows.Refusal as why:
        return BLOCK, (f"guard-paths: blocked: {why}. This is a plan-run rule "
                       "(docs/adr/0063-the-plan-run-enforcement-gate.md); do not look for another route. "
                       "Only the operator lifts it, by exporting HOOK_ALLOW_PATHS=1 before launching claude.")
    except (planrows.FactsError, planledger.LedgerError) as why:
        return BLOCK, (f"guard-paths: blocked: a review round cannot be recorded ({why}); refusing "
                       "(ADR 0063 §1, §5)")
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
    except Exception as err:  # fail closed: a recorder that cannot read the call blocks it (ADR 0001)
        print(f"guard-paths: blocked: a review pass could not be read ({type(err).__name__})", file=sys.stderr)
        return BLOCK
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
