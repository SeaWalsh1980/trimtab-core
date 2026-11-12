"""Shared fixtures for the enforcement-gate tests: a record table, and a decision for the mutation harness."""

from __future__ import annotations

import importlib
import unittest
from unittest import mock

# The owners the doctrine record gives each row, as it writes them.
_OWNERS = {
    **{f"E{n}": "Consuming project" for n in range(1, 5)},
    **{f"E{n}": "Guard" for n in (5, 6, 7, 8, 9, 10, 11, 12, 18, 19, 21, 23)},
    "E13": "Guard (plan-run layer)",
    **{f"E{n}": "guard-github" for n in range(14, 18)},
    "E20": "trimtab-core",
    "E22": "trimtab-core (job) and the consuming project's approver",
}
OWNERS = {rid: _OWNERS[rid] for rid in sorted(_OWNERS, key=lambda r: int(r[1:]))}
HEADER = "| ID | Condition | Fact it rests on | Owner |\n|---|---|---|---|\n"


def row_line(rid: str, condition: str | None = None, owner: str | None = None) -> str:
    return f"| {rid} | {condition or f'condition {rid}'} | fact for `{rid}` | {owner or OWNERS[rid]} |\n"


def record_text(ids=None, before: str = "# 0001 — a record\n\n## Enforcement\n\nIntro.\n\n",
                lines: str | None = None, after: str = "\nRequirements.\n\n## Options\n") -> str:
    body = lines if lines is not None else "".join(row_line(r) for r in (ids or OWNERS))
    return before + HEADER + body + after


# --- the mutation harness: does a row's test fail with its guard removed? -----------------

def survivors(function: str, allow, test_ids) -> list[str]:
    """The tests in `test_ids` not killed once `function` (module:name) is replaced by one that
    always returns `allow`. Only an assertion failure kills: a test that passes, errors or runs
    nothing does not show that it tests its guard."""
    module_name, name = function.split(":")
    module = importlib.import_module(module_name)
    loader = unittest.TestLoader()
    out = []
    with mock.patch.object(module, name, lambda *a, **k: allow):
        for test_id in test_ids:
            result = unittest.TestResult()
            loader.loadTestsFromName(test_id).run(result)
            if not result.failures or result.errors or not result.testsRun:
                out.append(test_id)
    return out


# A stand-in decision with one test that depends on it and one that doesn't, so the harness is
# itself shown to tell them apart.
ALLOW, REFUSE = 0, 2


def fixture_decision(command: str) -> int:
    return REFUSE if "forbidden" in command else ALLOW


class FixtureRefusals(unittest.TestCase):
    def test_refuses_the_forbidden_command(self):
        self.assertEqual(fixture_decision("a forbidden command"), REFUSE)

    def test_names_nothing_about_the_guard(self):
        self.assertEqual(ALLOW, 0)

    def test_errors_whatever_the_decision(self):
        raise RuntimeError("an error, not a failed assertion")
