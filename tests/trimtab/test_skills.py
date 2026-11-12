"""The plan skills point at each other by path (ADR 0065); every path must name a file that exists."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
PLAN_SKILLS = ("plan-run", "plan-item", "plan-pr")
# A cross-link names a skill, then a path inside it: plan-run/references/stops.md, plan-pr/SKILL.md.
LINK = re.compile(r"\b(plan-(?:run|item|pr)/[\w./-]*\.md)\b")


def skill_files():
    return sorted(p for name in PLAN_SKILLS for p in (SKILLS / name).rglob("*.md"))


class PlanSkillLinks(unittest.TestCase):
    def test_each_plan_skill_ships(self):
        self.assertEqual([n for n in PLAN_SKILLS if not (SKILLS / n / "SKILL.md").is_file()], [])

    def test_every_cross_link_names_a_file_that_exists(self):
        dangling = [(f.relative_to(ROOT).as_posix(), link)
                    for f in skill_files()
                    for link in LINK.findall(f.read_text(encoding="utf-8"))
                    if not (SKILLS / link).is_file()]

        self.assertEqual(dangling, [])

    def test_every_reference_file_is_linked_from_somewhere(self):
        """A reference no skill points at is a rule no run reads."""
        linked = {link for f in skill_files() for link in LINK.findall(f.read_text(encoding="utf-8"))}
        refs = [p.relative_to(SKILLS).as_posix() for name in PLAN_SKILLS for p in (SKILLS / name).glob("references/*.md")]

        self.assertEqual([r for r in refs if r not in linked], [])

    def test_no_skill_names_the_removed_wrap_merged(self):
        named = [f.relative_to(ROOT).as_posix() for f in SKILLS.rglob("*.md")
                 if "wrap-merged" in f.read_text(encoding="utf-8")]

        self.assertEqual(named, [])
        self.assertFalse((SKILLS / "wrap-merged").exists())


if __name__ == "__main__":
    unittest.main()
