"""Citations against a diff: the reviewer's deterministic pass (F2, R2, R3).

An earlier reviewer matched citations by the clause's text, so a formatting difference
made 34 correct citations "invented" (F2). Here a citation is an ID, and the
check is set arithmetic over the resolver's answer:

- a **binding path-scoped** item (REQUIRE/PROHIBIT, from a rule file whose
  `paths:` reach a changed file) that the plan did not cite was never in the
  planner's context: the reviewer grades it;
- a cited **path-scoped** item whose globs reach no changed file is out of
  scope (R3: flagged, never scored);
- always-loaded items (base rules, rule files without `paths:`) apply to every
  path, so scope cannot tell whether one binds this diff. They are counted and
  left to the reviewer's judgement, never reported as uncited.

Pure: the caller resolves the diff and parses the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from trimtab.registry.rules_for import RuleFile


@dataclass(frozen=True)
class CitationReport:
    cited: tuple[str, ...]
    cited_always: tuple[str, ...]
    in_scope_binding: tuple[str, ...]
    uncited_binding: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    unknown: int

    def line(self, plan_present: bool = True) -> str:
        if not plan_present:
            return (f"CITATIONS: plan=absent in_scope_binding={len(self.in_scope_binding)} "
                    f"uncited_binding={len(self.uncited_binding)}")
        return (f"CITATIONS: plan=present cited={len(self.cited)} always_loaded={len(self.cited_always)} "
                f"in_scope_binding={len(self.in_scope_binding)} uncited_binding={len(self.uncited_binding)} "
                f"out_of_scope={len(self.out_of_scope)} unknown={self.unknown}")


def check(cited_ids: Iterable[str], by_path: Mapping[str, Sequence[RuleFile]],
          all_rules: Sequence[RuleFile], unknown: int = 0) -> CitationReport:
    cited = sorted({i for i in cited_ids if i != "none"})
    scoped_in = {r.source: r for matched in by_path.values() for r in matched if not r.always}
    always = {i.id for r in all_rules if r.always for i in r.items}
    scoped_all = {i.id: r.source for r in all_rules if not r.always for i in r.items}
    binding = sorted(i.id for r in scoped_in.values() for i in r.items
                     if i.strength.binding and not i.withdrawn)
    return CitationReport(
        cited=tuple(cited),
        cited_always=tuple(i for i in cited if i in always),
        in_scope_binding=tuple(binding),
        uncited_binding=tuple(i for i in binding if i not in cited),
        out_of_scope=tuple(i for i in cited if i in scoped_all and scoped_all[i] not in scoped_in),
        unknown=unknown,
    )
