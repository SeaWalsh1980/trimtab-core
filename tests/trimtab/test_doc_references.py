"""Decision citations resolve inside this repository, never to an instance's records.

The base keeps its own ADR series (ADR 0008), separate from any instance's. A
citation copied from an instance's numbering either names a record the base
does not have or a different one, and nothing else in CI notices. Outside
docs/adr/, every tracked file must:

- name an ADR file by its full, existing file name, never a bare number path;
- cite only ADR numbers that exist here;
- cite no numbered plan or spec, and no plans directory: the base has none.

Text read inside other repositories (the SHIPPED directories), which number
their own ADRs, must also write every ADR number as `trimtab-core ADR NNNN`
(README "Contributing"). The check reads plural and listed cites (`ADRs
NNNN and NNNN`, one qualifier for the list) and lets a cite wrap once at a
space, but not past a blank line or a quote or comment marker. Strings the
CLI prints fall under the same README rule but are not checked here.

It cannot catch an in-range number used with another repository's meaning, or
an unnumbered plan reference; cite by full file name where the meaning matters.
A path written after `adr:` is exempt by that pattern, in any file: it is how
an overrides entry names the consumer's own ADR. Forbidden spellings are
assembled at run time so this file passes itself.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADR_DIR = "docs/" + "adr/"
ADR_PATH = re.compile(r"(adr:\s*)?" + re.escape(ADR_DIR) + r"(\d{4})(-[a-z0-9-]+\.md)?")
ADR_NUMBER = re.compile(r"\b" + "AD" + r"R[ -](\d{4})\b", re.IGNORECASE)
PRIVATE_RECORD = re.compile(r"\b(?:" + "pla" + "n|" + "spe" + r"c) \d{4}\b|" + re.escape("docs/" + "plans/"),
                            re.IGNORECASE)
SHIPPED = ("agents/", "commands/", "skills/", "templates/", "routines/templates/")
WRAP = r"(?:[ \t]+|[ \t]*\r?\n[ \t]*)"  # a space, or one line break with its indent
LIST_SEP = r"(?:," + WRAP + r"|,?" + WRAP + r"(?:and|or)" + WRAP + r")"
QUALIFIED_NUMBER = re.compile(r"((?<![\w-])trimtab-core" + WRAP + r")?(?i:\b" + "AD" + r"Rs?(?:-|" + WRAP + r")"
                              r"(\d{4})(?:" + LIST_SEP + r"\d{4})*\b)")
RULE = ("cite only this repository's ADRs, by existing number or full file name, "
        "and never an instance's ADR, plan or spec; under " + ", ".join(SHIPPED)
        + ", write trimtab-core AD" + "R NNNN")


def problems(text: str, adr_files: set[str]) -> list[str]:
    """Each citation in `text` that does not resolve against `adr_files` (file names)."""
    numbers = {name[:4] for name in adr_files}
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in ADR_PATH.finditer(line):
            if m.group(1):
                continue
            name = m.group(2) + (m.group(3) or "")
            if not m.group(3):
                found.append(f"line {n}: {ADR_DIR}{name} names no file")
            elif name not in adr_files:
                found.append(f"line {n}: {ADR_DIR}{name} does not exist")
        for m in ADR_NUMBER.finditer(line):
            if m.group(1) not in numbers:
                found.append(f"line {n}: ADR {m.group(1)} does not exist")
        for m in PRIVATE_RECORD.finditer(line):
            found.append(f"line {n}: {m.group(0)!r} cites a record this repository does not have")
    return found


def is_shipped(rel: str) -> bool:
    """Whether the tracked path `rel` is read inside other repositories."""
    return rel.startswith(SHIPPED)


def unqualified(text: str) -> list[str]:
    """Each ADR cite in `text` not written as `trimtab-core ADR NNNN`, by its first number."""
    found = []
    for m in QUALIFIED_NUMBER.finditer(text):
        if not m.group(1):
            line = text.count("\n", 0, m.start(2)) + 1
            found.append(f"line {line}: ADR {m.group(2)} needs the trimtab-core qualifier")
    return found


def tracked_files() -> list[str]:
    done = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True)
    return [p for p in done.stdout.decode("utf-8").split("\0") if p]


class Problems(unittest.TestCase):
    ADRS = {"0001-alpha.md", "0002-beta.md"}

    def path(self, name):
        return ADR_DIR + name

    def test_an_existing_full_file_name_passes(self):
        self.assertEqual(problems(f"See {self.path('0002-beta.md')}.", self.ADRS), [])

    def test_a_number_path_with_another_slug_fails(self):
        self.assertEqual(len(problems(f"See {self.path('0002-gamma.md')}.", self.ADRS)), 1)

    def test_a_bare_number_path_fails(self):
        self.assertEqual(len(problems(f"latencies in {self.path('0002')})", self.ADRS)), 1)

    def test_an_existing_adr_number_passes(self):
        self.assertEqual(problems("blocks (AD" + "R 0001); see trimtab-core AD" + "R 0002", self.ADRS), [])

    def test_an_adr_number_the_series_lacks_fails(self):
        self.assertEqual(len(problems("versioned (AD" + "R 0012)", self.ADRS)), 1)

    def test_a_hyphenated_adr_number_the_series_lacks_fails(self):
        self.assertEqual(len(problems("see AD" + "R-0012", self.ADRS)), 1)

    def test_a_lower_case_adr_number_the_series_lacks_fails(self):
        self.assertEqual(len(problems("see ad" + "r 0011", self.ADRS)), 1)

    def test_a_numbered_plan_fails(self):
        self.assertEqual(len(problems("see pla" + "n 0009's decision log", self.ADRS)), 1)

    def test_a_numbered_plan_starting_a_sentence_fails(self):
        self.assertEqual(len(problems("Pla" + "n 0009 records the trade.", self.ADRS)), 1)

    def test_a_numbered_spec_starting_a_sentence_fails(self):
        self.assertEqual(len(problems("Spe" + "c 0013 says so.", self.ADRS)), 1)

    def test_a_plans_directory_path_fails(self):
        self.assertEqual(len(problems("docs/" + "plans/0013-split.md", self.ADRS)), 1)

    def test_an_overrides_adr_field_is_exempt(self):
        self.assertEqual(problems("  adr: " + self.path("0009-consumer-own.md"), self.ADRS), [])


class Unqualified(unittest.TestCase):
    def test_a_bare_adr_number_fails_even_when_it_exists(self):
        self.assertEqual(len(unqualified("blocks (AD" + "R 0001)")), 1)

    def test_a_hyphenated_bare_adr_number_fails(self):
        self.assertEqual(len(unqualified("see AD" + "R-0001")), 1)

    def test_a_lower_case_bare_adr_number_fails(self):
        self.assertEqual(len(unqualified("see ad" + "r 0001")), 1)

    def test_a_qualified_adr_number_passes(self):
        self.assertEqual(unqualified("blocks (trimtab-core AD" + "R 0001)"), [])

    def test_a_qualifier_ending_the_line_before_passes(self):
        self.assertEqual(unqualified("reads the folder (trimtab-core\n   AD" + "R 0001): ok"), [])

    def test_another_word_before_the_number_fails(self):
        self.assertEqual(len(unqualified("the instance AD" + "R 0001")), 1)

    def test_a_qualifier_two_lines_before_fails(self):
        self.assertEqual(len(unqualified("trimtab-core\n\nAD" + "R 0001")), 1)

    def test_a_capitalised_qualifier_fails(self):
        self.assertEqual(len(unqualified("Trimtab-core AD" + "R 0001")), 1)

    def test_a_qualifier_with_a_lower_case_adr_passes(self):
        self.assertEqual(unqualified("trimtab-core ad" + "r 0001"), [])

    def test_a_qualifier_inside_a_longer_name_fails(self):
        self.assertEqual(len(unqualified("my-trimtab-core AD" + "R 0001")), 1)

    def test_a_qualifier_ending_a_crlf_line_passes(self):
        self.assertEqual(unqualified("(trimtab-core\r\n  AD" + "R 0001)"), [])

    def test_a_bare_plural_cite_fails(self):
        self.assertEqual(len(unqualified("AD" + "Rs 0001 and 0002")), 1)

    def test_a_qualified_plural_list_passes(self):
        self.assertEqual(unqualified("trimtab-core AD" + "Rs 0001, 0002 and 0003"), [])

    def test_a_number_wrapped_onto_the_next_line_fails(self):
        self.assertEqual(len(unqualified("see AD" + "R\n   0001")), 1)

    def test_a_cite_after_a_quote_marker_on_the_next_line_fails(self):
        self.assertEqual(len(unqualified("trimtab-core\n> AD" + "R 0001")), 1)

    def test_the_failure_names_the_line_of_a_wrapped_number(self):
        self.assertEqual(unqualified("one\nAD" + "R\n0001"), ["line 3: AD" + "R 0001 needs the trimtab-core qualifier"])

    def test_the_failure_names_the_line_of_the_number(self):
        self.assertEqual(unqualified("one\ntwo AD" + "R 0001"), ["line 2: AD" + "R 0001 needs the trimtab-core qualifier"])


class Shipped(unittest.TestCase):
    def test_each_shipped_directory_is_shipped(self):
        self.assertEqual([p for p in SHIPPED if not is_shipped(p + "x.md")], [])

    def test_a_nested_routine_template_is_shipped(self):
        self.assertTrue(is_shipped("routines/templates/retro.md"))

    def test_the_routines_readme_is_not_shipped(self):
        self.assertFalse(is_shipped("routines/README.md"))

    def test_a_directory_sharing_a_prefix_is_not_shipped(self):
        self.assertFalse(is_shipped("skills-extra/SKILL.md"))


class Repository(unittest.TestCase):
    def test_every_shipped_directory_holds_tracked_files(self):
        files = tracked_files()
        self.assertEqual([p for p in SHIPPED if not any(f.startswith(p) for f in files)], [],
                         "a shipped directory holds no tracked file; the qualifier check vouches for nothing there")

    def test_every_tracked_file_cites_only_this_repositorys_records(self):
        adr_files = {p.name for p in (REPO / ADR_DIR).glob("[0-9][0-9][0-9][0-9]-*.md")}
        self.assertTrue(adr_files, "no ADRs found; the check would vouch for nothing")
        found = []
        for rel in tracked_files():
            if rel.startswith(ADR_DIR):
                continue
            if not (REPO / rel).is_file():  # deleted but not yet staged: nothing to cite
                continue
            data = (REPO / rel).read_bytes()
            if b"\0" in data:  # binary, as git judges it: no citations to read
                continue
            text = data.decode("utf-8", errors="replace")  # a non-UTF-8 file is still scanned
            found += [f"{rel}: {p}" for p in problems(text, adr_files)]
            if is_shipped(rel):
                found += [f"{rel}: {p}" for p in unqualified(text)]
        self.assertEqual(found, [], f"{RULE}:\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
