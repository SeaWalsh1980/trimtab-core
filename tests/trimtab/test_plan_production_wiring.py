"""The facts the guard builds in production are a `LazyFacts`, not the fake every other test injects.

Round 3 found that `LazyFacts` delegated 13 of `Facts`' methods while `planacts` and `planchecks` call
more than 30 on it, so the real guard raised AttributeError on a second push and on every ready mark,
and no test saw it because each injected a fake that had the methods. These tests build the production
wiring (ADR 0063 §5, review round 3).
"""

import inspect
import unittest
from pathlib import Path

from trimtab import planfacts, planguard


class Recording:
    """Stands in for `GitHubFacts`: answers any method with its own name and records the call."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append(name)
            return name

        return call


def public_readers(cls):
    return sorted(n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n)))


class LazyFactsDelegatesEverything(unittest.TestCase):
    def test_every_public_reader_of_the_real_facts_is_reachable_through_the_lazy_wrapper(self):
        real = Recording()
        lazy = planguard.LazyFacts(Path("/work"), build=lambda cwd: real)

        for name in public_readers(planfacts.GitHubFacts):
            with self.subTest(name):
                needed = [p for p in inspect.signature(getattr(planfacts.GitHubFacts, name)).parameters.values()
                          if p.default is p.empty and p.name != "self"
                          and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
                self.assertEqual(getattr(lazy, name)(*["x"] * len(needed)), name)

        self.assertEqual(sorted(set(real.calls)), public_readers(planfacts.GitHubFacts))

    def test_nothing_is_built_until_a_fact_is_asked_for(self):
        built = []
        lazy = planguard.LazyFacts(Path("/work"), build=lambda cwd: built.append(cwd) or Recording())

        self.assertEqual(built, [])
        lazy.branch_sha("plan/x")
        lazy.merge_base_of("HEAD")

        self.assertEqual(built, [Path("/work")])  # once, however many facts

    def test_a_name_that_is_not_a_reader_is_not_invented(self):
        lazy = planguard.LazyFacts(Path("/work"), build=lambda cwd: Recording())

        with self.assertRaises(AttributeError):
            lazy._private_thing

    def test_the_support_the_guard_builds_without_injected_facts_holds_the_lazy_wrapper(self):
        # `PlanSupport(cwd)` with no `facts` is what guard-github constructs for a real call, so the
        # wrapper above is what every plan act then talks to.
        support = planguard.PlanSupport(Path("/work"), environ={})

        self.assertIsInstance(support.facts, planguard.LazyFacts)


if __name__ == "__main__":
    unittest.main()
