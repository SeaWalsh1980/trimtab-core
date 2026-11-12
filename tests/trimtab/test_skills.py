"""The plan skills point at each other by path (ADR 0065); every path must name a file that exists."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
PLAN_SKILLS = ("plan-run", "plan-item", "plan-pr")
# Each file's opening "Paths such as `…` are relative to …" line gives an example path, not a link.
ROOT_LINE = re.compile(r"Paths such as `[^`]*` are relative to the skills directory[^.]*\.")


def skill_dirs():
    return sorted(p.name for p in SKILLS.iterdir() if p.is_dir())


def link_pattern():
    # A cross-link names any shipped skill, then a path inside it: plan-run/references/stops.md,
    # plan-pr/SKILL.md, or the same with no extension.
    names = "|".join(re.escape(n) for n in skill_dirs())
    return re.compile(rf"(?<![\w./-])((?:{names})/[\w./-]*[\w-])")


def skill_files():
    return sorted(p for name in PLAN_SKILLS for p in (SKILLS / name).rglob("*.md"))


def links_in(path):
    """The cross-links a file makes, without its root line's example and without links to itself."""
    text = ROOT_LINE.sub("", path.read_text(encoding="utf-8"))
    own = path.relative_to(SKILLS).as_posix()
    return [link for link in link_pattern().findall(text) if link not in (own, own.removesuffix(".md"))]


def resolves(link):
    return (SKILLS / link).is_file() or (SKILLS / f"{link}.md").is_file()


class PlanSkillLinks(unittest.TestCase):
    def test_each_plan_skill_ships(self):
        self.assertEqual([n for n in PLAN_SKILLS if not (SKILLS / n / "SKILL.md").is_file()], [])

    def test_every_cross_link_names_a_file_that_exists(self):
        dangling = [(f.relative_to(ROOT).as_posix(), link)
                    for f in skill_files() for link in links_in(f) if not resolves(link)]

        self.assertEqual(dangling, [])

    def test_every_reference_file_is_linked_from_another_file(self):
        """A reference no other file points at is a rule no run reads."""
        linked = {link for f in skill_files() for link in links_in(f)}
        refs = [p.relative_to(SKILLS).as_posix() for name in PLAN_SKILLS
                for p in (SKILLS / name).glob("references/*.md")]

        self.assertEqual([r for r in refs if r not in linked and r.removesuffix(".md") not in linked], [])

    def test_every_file_states_what_its_paths_are_relative_to(self):
        self.assertEqual([f.relative_to(ROOT).as_posix() for f in skill_files()
                          if not ROOT_LINE.search(f.read_text(encoding="utf-8"))], [])

    def test_no_skill_names_the_removed_wrap_merged(self):
        named = [f.relative_to(ROOT).as_posix() for f in SKILLS.rglob("*.md")
                 if "wrap-merged" in f.read_text(encoding="utf-8")]

        self.assertEqual(named, [])
        self.assertFalse((SKILLS / "wrap-merged").exists())


if __name__ == "__main__":
    unittest.main()
