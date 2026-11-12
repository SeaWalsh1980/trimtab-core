"""The plan skills point at each other by path (ADR 0065).

Every path must name a file that exists, every reference must be reachable from a SKILL.md, and a
link to a file that holds an authorisation check, the stop protocol or the restart conditions must
say what an unreadable file means.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
PLAN_SKILLS = ("plan-run", "plan-item", "plan-pr")
# Each file's opening "Paths such as `…` are relative to …" line gives an example path, not a link.
ROOT_LINE = re.compile(r"Paths such as `[^`]*` are relative to the skills directory[^.]*\.")
# Anything shaped like a link into a skill, whatever the skill's name: <name>/SKILL.md or
# <name>/references/<file>, with or without .md. A misspelled or removed skill still matches.
LINK = re.compile(r"(?<![\w./-])([a-z][a-z0-9-]*/(?:SKILL(?:\.md)?|references/[\w.-]*\w))")
# Shapes a link must not take: relative to the repository, or naming no skill.
BAD_SHAPE = re.compile(r"(?<![\w./-])(?:skills/[a-z][\w-]*/|references/)")
FAIL_CLOSED = "If you cannot read it"
# The links each entry skill must make with FAIL_CLOSED in the same paragraph.
GUARDED = {
    "plan-run/SKILL.md": ("plan-run/references/authorisation.md", "plan-run/references/record-authority.md",
                          "plan-run/references/gate.md", "plan-run/references/restart.md",
                          "plan-run/references/stops.md"),
    "plan-item/SKILL.md": ("plan-run/references/stops.md",),
    "plan-pr/SKILL.md": ("plan-run/references/stops.md",),
}


def strip_root_line(text):
    return ROOT_LINE.sub("", text)


def links_in_text(text, own):
    """The cross-links a file's text makes, without its root line's example and without links to itself."""
    own_stem = own.removesuffix(".md")
    return [link for link in LINK.findall(strip_root_line(text)) if link.removesuffix(".md") != own_stem]


def resolve(link):
    """The file a link names, or None."""
    for candidate in (SKILLS / link, SKILLS / f"{link}.md"):
        if candidate.is_file():
            return candidate.relative_to(SKILLS).as_posix()
    return None


def paragraphs(text):
    """Blank-line paragraphs, with each list item its own paragraph, whitespace collapsed."""
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+\.)\s)", strip_root_line(text))
    return [" ".join(b.split()) for b in blocks if b.strip()]


def skill_files():
    return sorted(p for name in PLAN_SKILLS for p in (SKILLS / name).rglob("*.md"))


def rel(path):
    return path.relative_to(SKILLS).as_posix()


def read(path):
    return path.read_text(encoding="utf-8")


class LinkHelpers(unittest.TestCase):
    """The helpers the checks below rely on, on text written for the purpose."""

    def test_the_root_lines_example_is_not_a_link(self):
        text = "Paths such as `plan-run/references/stops.md` are relative to the skills directory, here."

        self.assertEqual(links_in_text(text, "plan-pr/SKILL.md"), [])

    def test_a_files_link_to_itself_is_not_counted(self):
        self.assertEqual(links_in_text("see plan-run/references/gate", "plan-run/references/gate.md"), [])

    def test_a_link_to_a_misspelled_skill_is_found_and_does_not_resolve(self):
        links = links_in_text("see plan-itm/references/records.md.", "plan-run/SKILL.md")

        self.assertEqual([(link, resolve(link)) for link in links], [("plan-itm/references/records.md", None)])

    def test_a_link_without_md_resolves_to_its_file(self):
        self.assertEqual(resolve("plan-run/references/stops"), "plan-run/references/stops.md")

    def test_a_repository_relative_or_skill_less_link_has_a_bad_shape(self):
        self.assertEqual(BAD_SHAPE.findall("see skills/plan-run/SKILL.md or references/stops.md"),
                         ["skills/plan-run/", "references/"])

    def test_a_list_item_is_its_own_paragraph(self):
        self.assertEqual(paragraphs("Intro:\n\n- one\n  more\n- two"), ["Intro:", "- one more", "- two"])


class PlanSkillLinks(unittest.TestCase):
    def test_each_plan_skill_ships(self):
        self.assertEqual([n for n in PLAN_SKILLS if not (SKILLS / n / "SKILL.md").is_file()], [])

    def test_the_skills_make_cross_links(self):
        """Guards the checks below against passing on an empty set."""
        self.assertGreater(sum(len(links_in_text(read(f), rel(f))) for f in skill_files()), 20)

    def test_every_cross_link_names_a_file_that_exists(self):
        dangling = [(rel(f), link) for f in skill_files() for link in links_in_text(read(f), rel(f))
                    if resolve(link) is None]

        self.assertEqual(dangling, [])

    def test_no_link_is_repository_relative_or_names_no_skill(self):
        bad = [(rel(f), shape) for f in skill_files() for shape in BAD_SHAPE.findall(strip_root_line(read(f)))]

        self.assertEqual(bad, [])

    def test_every_reference_is_reachable_from_a_skill_entry(self):
        """A reference no SKILL.md leads to, directly or through other references, is a rule no run reads."""
        reached, todo = set(), [f"{n}/SKILL.md" for n in PLAN_SKILLS]
        while todo:
            name = todo.pop()
            if name in reached:
                continue
            reached.add(name)
            todo += [r for link in links_in_text(read(SKILLS / name), name) if (r := resolve(link))]
        refs = sorted(rel(p) for n in PLAN_SKILLS for p in (SKILLS / n).glob("references/*.md"))

        self.assertTrue(refs)
        self.assertEqual([r for r in refs if r not in reached], [])

    def test_each_guarded_link_says_what_an_unreadable_file_means(self):
        missing = []
        for entry, targets in GUARDED.items():
            paras = paragraphs(read(SKILLS / entry))
            for target in targets:
                if not any(FAIL_CLOSED in p and target in [resolve(link) for link in links_in_text(p, entry)]
                           for p in paras):
                    missing.append((entry, target))

        self.assertEqual(missing, [])

    def test_every_file_states_what_its_paths_are_relative_to(self):
        self.assertEqual([rel(f) for f in skill_files() if not ROOT_LINE.search(read(f))], [])

    def test_no_skill_names_the_removed_wrap_merged(self):
        named = [rel(f) for f in SKILLS.rglob("*.md") if "wrap-merged" in read(f)]

        self.assertEqual(named, [])
        self.assertFalse((SKILLS / "wrap-merged").exists())


if __name__ == "__main__":
    unittest.main()
