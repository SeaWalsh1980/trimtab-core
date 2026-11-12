"""The plan skills point at each other by path (ADR 0067).

Every path must name a file that exists, every reference must be reachable from a SKILL.md, each
SKILL.md must carry the shared rule for a linked file it cannot read, and a link to a file that
decides something must say what an unreadable copy of it means.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
PLAN_SKILLS = ("plan-run", "plan-item", "plan-pr")
ENTRIES = tuple(f"{name}/SKILL.md" for name in PLAN_SKILLS)

# Each file's opening line names an example path; it is not a link.
ROOT_LINE = re.compile(r"Paths\s+such\s+as\s+`[^`]*`\s+are\s+relative\s+to\s+the\s+skills\s+directory,\s+"
                       r"(?:the\s+parent\s+of\s+this\s+skill's\s+own\s+directory|two\s+levels\s+above\s+this\s+file)\.")
# An extensionless link into a skill, whatever the skill's name: <name>/SKILL or <name>/references/<file>.
BARE_LINK = re.compile(r"(?<![\w./-])([a-z][a-z0-9-]*/(?:SKILL|references/[\w-]*\w))(?![\w-])(?!\.\w)")
# Any mention of a Markdown file, with whatever path is in front of it.
MD_MENTION = re.compile(r"(?<![\w./<>{}*-])([\w./<>{}*-]*\.md)\b")

SHARED_RULE = " ".join("""
**If you cannot read a file this skill links to, that is a stop:** follow
plan-run/references/stops.md. If you cannot read that either, make no file write; in a plan run,
label `plan:blocked` and remove any `plan:continue` on the open item PR or, with none open, on the
merged PR that started the session; and report the blocker, naming the file, with one to three ways
forward. Where a link below says more about an unreadable file, that adds to this rule.
""".split())

# A link to one of these files must say, in its own paragraph, what an unreadable copy means.
OWN_MEANING = {
    "plan-run/references/authorisation.md": "the check fails: stop",
    "plan-run/references/record-authority.md": "the record authorises nothing: stop",
    "plan-run/references/gate.md": "the gate fails",
    "plan-run/references/restart.md": "no session is a restart",
}
# The entry must make each of these links at least once with its meaning.
OWN_MEANING_IN = {"plan-run/SKILL.md": tuple(OWN_MEANING)}
# Every link to these, in any plan skill file, must carry its meaning.
EVERY_LINK_NEEDS_MEANING = ("plan-run/references/restart.md",)

# More links than this means the scan is reading the files; the real count is about forty.
MIN_LINKS = 20


def strip_root_line(text):
    return ROOT_LINE.sub("", text)


def mentions(text):
    """Every path-like mention of a skill file in text, without the root line's example."""
    text = strip_root_line(text)
    return MD_MENTION.findall(text) + BARE_LINK.findall(text)


def resolve(link):
    """The skills-relative file a link names, or None. A link must be written relative to skills/."""
    if link.startswith(("./", "../", "/")) or "<" in link:
        return None
    for candidate in (SKILLS / link, SKILLS / f"{link}.md"):
        if candidate.is_file():
            return candidate.relative_to(SKILLS).as_posix()
    return None


def skill_basenames():
    return {p.name for name in PLAN_SKILLS for p in (SKILLS / name).rglob("*.md")}


def broken(text):
    """Mentions in text that look like links to skill files but don't resolve."""
    out = []
    for m in mentions(text):
        if "<" in m:
            continue  # a placeholder such as <plan>.md, not a link
        if "/" in m:
            if resolve(m) is None:
                out.append(m)
        elif m in skill_basenames():
            out.append(m)  # a bare SKILL.md or stops.md names no skill
    return out


def links_in(text, own):
    """The files text links to, without links to itself."""
    found = (resolve(m) for m in mentions(text) if "/" in m)
    return [r for r in found if r and r != own]


def paragraphs(text):
    """Blank-line paragraphs, with each list item its own paragraph, whitespace collapsed."""
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+\.)\s)", strip_root_line(text))
    return [" ".join(b.split()) for b in blocks if b.strip()]


def reachable(entries, links_of):
    """Every file reachable from entries by following links_of(name)."""
    seen, todo = set(), list(entries)
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += links_of(name)
    return seen


def without_meaning(text, own, targets, every):
    """Each target that text never links to with its meaning in the same paragraph.

    A target in `every` must have its meaning in every paragraph that links it.
    """
    paras = [(p, links_in(p, own)) for p in paragraphs(text)]
    missing = []
    for target in targets:
        linking = [p for p, links in paras if target in links]
        meant = [p for p in linking if OWN_MEANING[target] in p]
        if not meant or (target in every and len(meant) < len(linking)):
            missing.append(target)
    return missing


def skill_files():
    return sorted(p for name in PLAN_SKILLS for p in (SKILLS / name).rglob("*.md"))


def rel(path):
    return path.relative_to(SKILLS).as_posix()


def read(name):
    return (SKILLS / name).read_text(encoding="utf-8")


class Helpers(unittest.TestCase):
    """The helpers the checks below rely on, on text written for the purpose."""

    def test_the_root_lines_example_is_not_a_link(self):
        text = ("Paths such as `plan-run/references/stops.md` are relative to the skills directory, "
                "the parent of this skill's own directory.")

        self.assertEqual(mentions(text), [])

    def test_a_link_to_itself_is_not_counted(self):
        self.assertEqual(links_in("see plan-run/references/gate.md", "plan-run/references/gate.md"), [])

    def test_a_misspelled_skill_or_directory_is_broken(self):
        self.assertEqual(broken("see plan-itm/SKILL.md and plan-run/refrences/stops.md."),
                         ["plan-itm/SKILL.md", "plan-run/refrences/stops.md"])

    def test_a_dot_relative_or_repository_relative_link_is_broken(self):
        self.assertEqual(broken("see ../plan-run/SKILL.md, ./references/gate.md or skills/plan-pr/SKILL.md"),
                         ["../plan-run/SKILL.md", "./references/gate.md", "skills/plan-pr/SKILL.md"])

    def test_a_bare_skill_file_name_is_broken_and_a_placeholder_is_not(self):
        self.assertEqual(broken("SKILL.md, stops.md, <plan>.md and CLAUDE.md"), ["SKILL.md", "stops.md"])

    def test_an_extensionless_link_to_a_missing_file_is_broken(self):
        self.assertEqual(broken("see plan-run/references/nowhere."), ["plan-run/references/nowhere"])

    def test_a_link_without_md_resolves_to_its_file(self):
        self.assertEqual(resolve("plan-run/references/stops"), "plan-run/references/stops.md")

    def test_a_list_item_is_its_own_paragraph(self):
        self.assertEqual(paragraphs("Intro:\n\n- one\n  more\n- two"), ["Intro:", "- one more", "- two"])

    def test_two_files_that_link_only_each_other_are_not_reached(self):
        graph = {"a": ["b"], "b": [], "x": ["y"], "y": ["x"]}

        self.assertEqual(reachable(["a"], graph.get), {"a", "b"})

    def test_a_link_without_its_meaning_is_reported(self):
        text = "See plan-run/references/gate.md.\n\nSee plan-run/references/restart.md. If you cannot read it, go on."

        self.assertEqual(without_meaning(text, "x.md", ["plan-run/references/gate.md",
                                                        "plan-run/references/restart.md"], ()),
                         ["plan-run/references/gate.md", "plan-run/references/restart.md"])

    def test_another_links_meaning_does_not_count(self):
        text = "See plan-run/references/gate.md and plan-run/references/restart.md: if not, the gate fails."

        self.assertEqual(without_meaning(text, "x.md", ["plan-run/references/restart.md"], ()),
                         ["plan-run/references/restart.md"])

    def test_every_link_needs_its_meaning_when_the_target_says_so(self):
        target = "plan-run/references/restart.md"
        text = f"See {target}. If you cannot read it, no session is a restart.\n\nSee {target} again."

        self.assertEqual(without_meaning(text, "x.md", [target], ()), [])
        self.assertEqual(without_meaning(text, "x.md", [target], (target,)), [target])


class PlanSkillLinks(unittest.TestCase):
    def test_each_plan_skill_ships(self):
        self.assertEqual([e for e in ENTRIES if not (SKILLS / e).is_file()], [])

    def test_the_skills_make_cross_links(self):
        """Guards the checks below against passing on an empty set."""
        self.assertGreater(sum(len(links_in(read(rel(f)), rel(f))) for f in skill_files()), MIN_LINKS)

    def test_no_mention_of_a_skill_file_is_broken(self):
        self.assertEqual([(rel(f), m) for f in skill_files() for m in broken(read(rel(f)))], [])

    def test_every_reference_is_reachable_from_a_skill_entry(self):
        """A reference no SKILL.md leads to, directly or through other references, is a rule no run reads."""
        seen = reachable(ENTRIES, lambda name: links_in(read(name), name))
        refs = sorted(rel(p) for name in PLAN_SKILLS for p in (SKILLS / name).glob("references/**/*.md"))

        self.assertTrue(refs)
        self.assertEqual([r for r in refs if r not in seen], [])

    def test_each_skill_entry_carries_the_shared_rule_for_an_unreadable_link(self):
        self.assertEqual([e for e in ENTRIES if SHARED_RULE not in " ".join(read(e).split())], [])

    def test_the_entry_gives_each_deciding_link_its_meaning(self):
        missing = [(f, t) for f, targets in OWN_MEANING_IN.items()
                   for t in without_meaning(read(f), f, targets, EVERY_LINK_NEEDS_MEANING)]

        self.assertEqual(missing, [])

    def test_every_link_to_the_restart_rule_says_no_restart_when_unreadable(self):
        missing = [(rel(f), t) for f in skill_files() for t in EVERY_LINK_NEEDS_MEANING
                   if t in links_in(read(rel(f)), rel(f))
                   and without_meaning(read(rel(f)), rel(f), [t], EVERY_LINK_NEEDS_MEANING)]

        self.assertEqual(missing, [])

    def test_every_file_states_what_its_paths_are_relative_to(self):
        self.assertEqual([rel(f) for f in skill_files() if not ROOT_LINE.search(read(rel(f)))], [])

    def test_no_skill_names_the_removed_wrap_merged(self):
        self.assertEqual([rel(f) for f in SKILLS.rglob("*.md") if "wrap-merged" in read(rel(f))], [])

    def test_the_removed_wrap_merged_directory_is_gone(self):
        self.assertFalse((SKILLS / "wrap-merged").exists())


if __name__ == "__main__":
    unittest.main()
