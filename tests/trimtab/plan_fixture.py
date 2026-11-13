"""A hermetic instance, config directory and ledger for the guard tests that reach the plan-run ledger.

guard-github now records an allowed `plan/*` push in the part's ledger and judges E18 against the
authorised plan at the merge-base (ADR 0063 §1, §5). Those reads must never touch the machine's real
`~/.claude` or its instance (TST-1), so a module that exercises them installs this fixture.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from instance_fixture import make_instance
from trimtab import planledger, planscope
from trimtab.enforcement import guardgate

REPO = "owner/proj"
PLAN = "0008-slug"
ITEM, PART = 2, 1
FLOWS = ".github/" + "workflows/"
# The plan's item 2 names no marker and no threshold line, so E18's exception never applies by accident.
PLAN_TEXT = "1. First.\n2. Second.\n"
KEYS = {
    "accounts": {"admin": "the-admin", "agent": "claude-bot"},
    "secret_scan": {"workflow": FLOWS + "scan.yml", "job": "secret-scan"},
    "review": {
        "run": {"workflow": FLOWS + "review.yml", "job": "review", "event": "pull_request"},
        "facts": {"workflow": FLOWS + "facts.yml", "job": "facts"},
        "login": "review-bot[bot]", "rerequest_label": "review:run",
        "tags": {"fixed": "[fixed]", "wrong": "[wrong]", "intentional": "[intentional]", "moot": "[moot]",
                 "deferred": "[deferred]"},
    },
    "fix_limits": {"markers": ["pytest.skip("], "thresholds": ["fail_under"]},
}

# TST-4: a fixed clock, so nothing a test writes depends on the time it ran.
WHEN = "2026-11-13T10:00:00+00:00"

_state: dict[str, object] = {}


def set_up(plan=PLAN, item=ITEM, part=PART) -> None:
    """Install the fixture: an instance with the review keys, a private config directory, a started
    ledger for the part, and a stub that answers the authorised plan's text.

    Several modules in one run each install it; the first one wins and the count decides when the last
    tear-down undoes it, so no module leaves another's environment patched."""
    _state["depth"] = int(_state.get("depth", 0)) + 1
    if _state["depth"] > 1:
        return
    tmp = Path(tempfile.mkdtemp())
    root = make_instance(tmp)
    body = json.loads((root / "instance.json").read_text())
    (root / "instance.json").write_text(json.dumps({**body, **KEYS}))
    config = tmp / "config"
    config.mkdir(mode=0o700)
    planledger.Ledger(config, REPO, plan, item, part, clock=lambda: WHEN).start(remote_branch_exists=False)
    env = mock.patch.dict(os.environ, {"TRIMTAB_INSTANCE": str(root), "CLAUDE_CONFIG_DIR": str(config)})
    env.start()
    reader = mock.patch.object(planscope, "read_files",
                               lambda repo, sha, paths: {planscope.plan_files(plan)[0]: PLAN_TEXT})
    reader.start()
    # E21 is tested on its own (test_plan_gate); here the gate stands passed, so each spelling test
    # judges the row it is about and no test reads the machine's own enforcement cache.
    passed = mock.patch.object(guardgate, "holds", lambda project, *a, **kw: True)
    passed.start()
    _state["passed"] = passed
    _state["tmp"], _state["env"], _state["reader"], _state["config"] = tmp, env, reader, config


def tear_down() -> None:
    _state["depth"] = max(0, int(_state.get("depth", 0)) - 1)
    if _state["depth"]:
        return
    for key in ("passed", "reader", "env"):
        if key in _state:
            _state.pop(key).stop()
    if "tmp" in _state:
        shutil.rmtree(_state.pop("tmp"), ignore_errors=True)
    _state.pop("config", None)


def ledger(plan=PLAN, item=ITEM, part=PART) -> planledger.Ledger:
    """The fixture's ledger, for a test that reads what a guard wrote to it."""
    return planledger.Ledger(_state["config"], REPO, plan, item, part, clock=lambda: WHEN)


def own_part(case) -> tuple[str, planledger.Ledger]:
    """A part of this test's own, started, with its branch: one ledger per test, so no test reads the
    events another wrote (TST-1 forbids shared mutable state and order dependence).

    The part number comes from the test's own name, so two tests never collide and a test's number does
    not move when another is added."""
    import hashlib
    name = f"{type(case).__module__}.{type(case).__name__}.{case._testMethodName}"
    part = 1 + int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 100000
    found = ledger(part=part)
    found.start(remote_branch_exists=False)
    return f"plan/{PLAN}/item-{ITEM:02d}-part{part}", found
