"""Decision citations resolve inside this repository, never to an instance's records.

The base keeps its own ADR series (ADR 0008), separate from any instance's. A
citation copied from an instance's numbering either names a record the base
does not have or a different one, and nothing else in CI notices. Outside
docs/adr/, every tracked file must:

- name an ADR file by its full, existing file name, never a bare number path;
- cite only ADR numbers that exist here;
- cite no numbered plan or spec, and no plans directory: the base has none.

It cannot catch an in-range number used with another repository's meaning, or
an unnumbered plan reference; cite by full file name where the meaning matters.
The `adr:` field of an overrides entry names the consumer's own ADR and is
exempt. Forbidden spellings are assembled at run time so this file passes itself.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADR_DIR = "docs/" + "adr/"
ADR_PATH = re.compile(r"(adr:\s*)?" + re.escape(ADR_DIR) + r"(\d{4})(-[a-z0-9-]+\.md)?")
ADR_NUMBER = re.compile(r"\b" + "AD" + r"R[ -](\d{4})\b")
PRIVATE_RECORD = re.compile(r"\b(?:" + "pla" + "n|" + "spe" + r"c) \d{4}\b|" + re.escape("docs/" + "plans/"))
RULE = ("cite only this repository's ADRs, by existing number or full file name, "
        "and never an instance's ADR, plan or spec")


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

    def test_a_numbered_plan_fails(self):
        self.assertEqual(len(problems("see pla" + "n 0009's decision log", self.ADRS)), 1)

    def test_a_plans_directory_path_fails(self):
        self.assertEqual(len(problems("docs/" + "plans/0013-split.md", self.ADRS)), 1)

    def test_an_overrides_adr_field_is_exempt(self):
        self.assertEqual(problems("  adr: " + self.path("0009-consumer-own.md"), self.ADRS), [])


class Repository(unittest.TestCase):
    def test_every_tracked_file_cites_only_this_repositorys_records(self):
        adr_files = {p.name for p in (REPO / ADR_DIR).glob("[0-9][0-9][0-9][0-9]-*.md")}
        self.assertTrue(adr_files, "no ADRs found; the check would vouch for nothing")
        found = []
        for rel in tracked_files():
            if rel.startswith(ADR_DIR):
                continue
            data = (REPO / rel).read_bytes()
            if b"\0" in data:  # binary, as git judges it: no citations to read
                continue
            found += [f"{rel}: {p}" for p in problems(data.decode("utf-8"), adr_files)]
        self.assertEqual(found, [], f"{RULE}:\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
