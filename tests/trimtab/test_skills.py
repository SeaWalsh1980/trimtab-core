"""The plan skills point at each other by path (ADRs 0068, 0069).

Every path must name a file that exists (no `..`, and an anchor must name a heading), every
reference must be reachable from a SKILL.md, each SKILL.md must carry the shared rule for a linked
file it cannot read above its first link, and a link to a file that decides something must say,
after "If you cannot read it", what an unreadable copy of it means.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"
PLAN_SKILLS = ("plan-run", "plan-item", "plan-pr")
ENTRIES = tuple(f"{name}/SKILL.md" for name in PLAN_SKILLS)

# Each file's opening line names an example path; it is not a link. The wording is pinned on
# purpose: the scan strips exactly this line before it looks for links, so a reworded line would
# turn its example into a link the checks count. Each wording belongs to one file type: a SKILL.md
# sits in its skill's directory, a reference two levels down.
ROOT_LINE_START = r"Paths\s+such\s+as\s+`[^`]*`\s+are\s+relative\s+to\s+the\s+skills\s+directory,\s+"
ENTRY_ROOT = r"the\s+parent\s+of\s+this\s+skill's\s+own\s+directory\."
REFERENCE_ROOT = r"two\s+levels\s+above\s+this\s+file\."
ROOT_LINE = re.compile(ROOT_LINE_START + f"(?:{ENTRY_ROOT}|{REFERENCE_ROOT})")
ROOT_LINE_FOR = {"entry": re.compile(ROOT_LINE_START + ENTRY_ROOT),
                 "reference": re.compile(ROOT_LINE_START + REFERENCE_ROOT)}
# An extensionless link into a skill, whatever the skill's name: <name>/SKILL or <name>/references/<file>.
BARE_LINK = re.compile(r"(?<![\w./-])([a-z][a-z0-9-]*/(?:SKILL|references/[\w-]*\w))(?![\w-])(?!\.\w)")
# Any mention of a Markdown file, with whatever path is in front of it.
MD_MENTION = re.compile(r"(?<![\w./<>{}*-])([\w./<>{}*-]*\.md)\b")
# A link to a heading inside a Markdown file.
ANCHOR_LINK = re.compile(r"(?<![\w./<>{}*-])([\w./-]*\.md)#([\w-]*)")

SHARED_RULE = " ".join("""
**If you cannot read a file this skill links to, that is a stop:** follow
plan-run/references/stops.md. If you cannot read that either, make no file write; in a plan run,
label `plan:blocked` and remove any `plan:continue` on the open item PR or, with none open, on the
merged PR whose progress record supplied the next work (the plan's own PR for item 01, or the
merged PR that started the session if you have not found that record yet); and report the
blocker, naming the file, with one to three ways forward. The record's `## Next` is not updated
then, so where it and the label disagree, the label is authoritative. Where a link below says more
about an unreadable file, that adds to this rule.
""".split())

# What says that an unreadable link's meaning follows: the meaning must come right after it.
IF_UNREADABLE = "If you cannot read it, "

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
    if link.startswith(("./", "/")) or "<" in link or ".." in link.split("/"):
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
    for path, anchor in ANCHOR_LINK.findall(strip_root_line(text)):
        target = resolve(path)
        if target and anchor not in heading_anchors(read(target)):
            out.append(f"{path}#{anchor}")
    return out


def heading_anchors(text):
    """The anchors GitHub gives a file's headings: lower case, punctuation dropped, spaces as hyphens."""
    out = []
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, flags=re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        out.append(slug)
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
        meant = [p for p in linking if IF_UNREADABLE + OWN_MEANING[target] in p]
        if not meant or (target in every and len(meant) < len(linking)):
            missing.append(target)
    return missing


def text_before_shared_rule(text):
    """The entry's text, root line stripped, before the shared rule; None when the rule is missing."""
    flat = " ".join(strip_root_line(text).split())
    at = flat.find(SHARED_RULE)
    return None if at < 0 else flat[:at]


def file_type(name):
    return "entry" if name.endswith("/SKILL.md") else "reference"


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

    def test_a_link_that_climbs_with_dot_dot_is_broken(self):
        self.assertEqual(broken("see plan-run/../plan-pr/SKILL.md"), ["plan-run/../plan-pr/SKILL.md"])

    def test_an_anchor_to_a_missing_heading_is_broken(self):
        self.assertEqual(broken("see plan-run/SKILL.md#nowhere"), ["plan-run/SKILL.md#nowhere"])

    def test_an_anchor_to_a_real_heading_is_not_broken(self):
        self.assertEqual(broken("see plan-run/SKILL.md#1-confirm-the-merge"), [])

    def test_a_headings_anchor_drops_punctuation_and_hyphenates_spaces(self):
        self.assertEqual(heading_anchors("# Top\n## 1. Confirm the merge\n### `x` y's"),
                         ["top", "1-confirm-the-merge", "x-ys"])

    def test_a_meaning_not_tied_to_an_unreadable_file_does_not_count(self):
        text = "See plan-run/references/gate.md: if a condition fails, the gate fails."

        self.assertEqual(without_meaning(text, "x.md", ["plan-run/references/gate.md"], ()),
                         ["plan-run/references/gate.md"])

    def test_a_link_above_the_shared_rule_is_found(self):
        text = f"See plan-run/references/gate.md first.\n\n{SHARED_RULE}"

        self.assertEqual(links_in(text_before_shared_rule(text), "x.md"), ["plan-run/references/gate.md"])

    def test_a_missing_shared_rule_has_no_text_before_it(self):
        self.assertIsNone(text_before_shared_rule("no rule here"))

    def test_each_root_line_wording_matches_only_its_own_file_type(self):
        entry = ("Paths such as `plan-run/references/stops.md` are relative to the skills directory, "
                 "the parent of this skill's own directory.")
        reference = ("Paths such as `plan-run/SKILL.md` are relative to the skills directory, "
                     "two levels above this file.")

        self.assertEqual([bool(ROOT_LINE_FOR[t].search(w)) for t in ("entry", "reference")
                          for w in (entry, reference)], [True, False, False, True])


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

    def test_every_file_states_what_its_paths_are_relative_to_in_its_own_types_wording(self):
        wrong = [rel(f) for f in skill_files()
                 if not ROOT_LINE_FOR[file_type(rel(f))].search(read(rel(f)))
                 or any(ROOT_LINE_FOR[t].search(read(rel(f))) for t in ROOT_LINE_FOR if t != file_type(rel(f)))]

        self.assertEqual(wrong, [])

    def test_the_shared_rule_sits_above_each_entrys_first_link(self):
        """A run that stops reading at the first link it cannot follow must already have the rule."""
        before = {e: text_before_shared_rule(read(e)) for e in ENTRIES}

        self.assertEqual([e for e, text in before.items() if text is None or mentions(text)], [])

    def test_no_skill_names_the_removed_wrap_merged(self):
        self.assertEqual([rel(f) for f in SKILLS.rglob("*.md") if "wrap-merged" in read(rel(f))], [])

    def test_the_removed_wrap_merged_directory_is_gone(self):
        self.assertFalse((SKILLS / "wrap-merged").exists())


if __name__ == "__main__":
    unittest.main()
