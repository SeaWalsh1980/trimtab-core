"""The shipped commands, agents and templates reach GitHub through repository-scoped REST only.

gh's porcelain goes through GraphQL, which Claude Code cloud sessions refuse,
and those sessions refuse every REST path outside `repos/{owner}/{repo}/...`
too, `search/` included: a command that used either could not run in a
routine. Reads this checkout's own files; writes nothing.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHIPPED = ("commands", "agents", "templates", "routines/templates")
GRAPHQL = re.compile(
    r"\bgh\s+(?:pr\s+(?:list|view|create|edit|checks|merge|ready|comment|status)"
    r"|repo\s+view|issue\s+(?:list|view|create|edit|comment)|api\s+graphql)\b"
)
# `gh api`, any method, header or --paginate flags, then the path it calls.
GH_API_PATH = re.compile(
    r"""\bgh\s+api((?:\s+(?:-X|--method)\s+[A-Z]+|\s+-H\s+(?:"[^"]*"|'[^']*'|\S+)|\s+--paginate)*)"""
    r"""\s+["']?(?P<path>[^\s"'`]+)"""
)
# GitHub search syntax: a `--search` flag or a search qualifier. Only the search
# API reads it, and cloud sessions refuse that API.
SEARCH_SYNTAX = re.compile(r"--search\b|\bin:title\b|\bis:(?:pr|issue|merged|open|closed)\b")
# A shipped file that opens a PR carries the rule that keeps the run's own
# reference out of what it writes. A `gh api` call that POSTs to `pulls`,
# whatever the flag's spelling or place.
GH_API = re.compile(r"\bgh\s+api\b")
POST = re.compile(r"(?:-X|--method)\s+POST\b")
PULLS = re.compile(r"\bpulls\b")
NO_RUN_REFERENCE = "`Claude-Session:`"
# Every copy of the rule carries this sentence, compared with whitespace
# collapsed, so one copy cannot drift from the rest unnoticed.
NO_RUN_REFERENCE_SENTENCES = ("This overrides any default attribution",
                              "never commit past a failure")
# A shipped file that commits has the run check what it commits first.
COMMITS = re.compile(r"\bgit\s+commit\b")
# The commands that commit in prose ("Commit only the report") as well as by
# a literal `git commit`: named, since a pattern cannot tell prose from a step.
COMMITTING_COMMANDS = ("commands/trimtab-bump.md", "commands/trimtab-conformance.md",
                       "commands/trimtab-retro.md", "commands/trimtab-upstream-retro.md")
IDS_CHECK = "`trimtab ids --staged`"


def shipped_files():
    for top in SHIPPED:
        root = REPO / top
        if root.is_dir():
            yield from sorted(p for p in root.rglob("*") if p.is_file())


def graphql_lines(text):
    return [n for n, line in enumerate(text.splitlines(), 1) if GRAPHQL.search(line)]


def unscoped_lines(text):
    """Lines holding a `gh api` call whose path is not under `repos/`."""
    return [n for n, line in enumerate(text.splitlines(), 1)
            if any(not m.group("path").lstrip("/").startswith("repos/") for m in GH_API_PATH.finditer(line))]


class NoShippedFileUsesGhPorcelain(unittest.TestCase):
    def test_the_shipped_directories_are_found(self):
        self.assertTrue(any(True for _ in shipped_files()))

    def test_no_shipped_file_calls_a_graphql_backed_gh_command(self):
        found = {str(p.relative_to(REPO)): graphql_lines(p.read_text(encoding="utf-8")) for p in shipped_files()}

        self.assertEqual({path: lines for path, lines in found.items() if lines}, {})

    def test_the_check_catches_porcelain(self):
        lines = ["gh pr list --state merged", "gh repo view --json name", "gh issue create -t x",
                 "gh api graphql -f query=x", "gh pr create --draft"]

        self.assertEqual(graphql_lines("\n".join(lines)), [1, 2, 3, 4, 5])

    def test_the_check_lets_rest_through(self):
        lines = ["gh api 'repos/{owner}/{repo}' --jq .full_name", "gh api -X POST repos/o/r/pulls -F body=@f"]

        self.assertEqual(graphql_lines("\n".join(lines)), [])


class NoShippedFileCallsAPathOutsideRepos(unittest.TestCase):
    def test_no_shipped_file_calls_a_path_outside_repos(self):
        found = {str(p.relative_to(REPO)): unscoped_lines(p.read_text(encoding="utf-8")) for p in shipped_files()}

        self.assertEqual({path: lines for path, lines in found.items() if lines}, {})

    def test_the_check_catches_paths_outside_repos(self):
        lines = ["gh api -X GET search/issues -f q=x", "gh api user --jq .login", "gh api /orgs/o/repos",
                 'gh api -H "Accept: x" notifications', "gh api --paginate 'search/code?q=x'",
                 "X=$(gh api 'repos/{owner}/{repo}' --jq .full_name) && gh api -X GET search/issues"]

        self.assertEqual(unscoped_lines("\n".join(lines)), [1, 2, 3, 4, 5, 6])

    def test_the_check_lets_repository_paths_through(self):
        lines = ["gh api 'repos/{owner}/{repo}' --jq .full_name", 'gh api "repos/$REPO/pulls?state=open"',
                 "gh api -X POST repos/o/r/pulls -F body=@f", "gh api /repos/o/r/issues",
                 "gh api 'repos/{owner}/{repo}/pulls/<n>/files' --paginate --jq .", "`gh api` on a REST path"]

        self.assertEqual(unscoped_lines("\n".join(lines)), [])


def search_lines(text):
    return [n for n, line in enumerate(text.splitlines(), 1) if SEARCH_SYNTAX.search(line)]


class NoShippedFileUsesSearchSyntax(unittest.TestCase):
    def test_no_shipped_file_uses_search_syntax(self):
        found = {str(p.relative_to(REPO)): search_lines(p.read_text(encoding="utf-8")) for p in shipped_files()}

        self.assertEqual({path: lines for path, lines in found.items() if lines}, {})

    def test_the_check_catches_search_syntax(self):
        lines = ['(`--search "structure in:title updated:>=x"`)', "q=repo:o/r is:pr is:merged",
                 "gh label list --search harness"]

        self.assertEqual(search_lines("\n".join(lines)), [1, 2, 3])

    def test_the_check_lets_listings_through(self):
        lines = ['gh api "repos/$REPO/pulls?state=closed&sort=updated"', "never `search/…` either",
                 "the title starts `[<ID>]`"]

        self.assertEqual(search_lines("\n".join(lines)), [])


def opens_a_pr(text):
    return any(GH_API.search(line) and POST.search(line) and PULLS.search(line) for line in text.splitlines())


class EveryShippedFileThatOpensAPrKeepsTheRunOut(unittest.TestCase):
    def test_every_file_that_opens_a_pr_forbids_a_run_reference(self):
        opening = [p for p in shipped_files() if opens_a_pr(p.read_text(encoding="utf-8"))]

        missing = [str(p.relative_to(REPO)) for p in opening if NO_RUN_REFERENCE not in p.read_text(encoding="utf-8")]

        self.assertTrue(opening)
        self.assertEqual(missing, [])

    def test_a_pr_is_opened_whatever_the_flags_spelling_or_place(self):
        for line in ("gh api -X POST 'repos/{owner}/{repo}/pulls' -F body=@f",
                     'gh api "repos/$REPO/pulls" -X POST -F body=@f',
                     "gh api --method POST repos/o/r/pulls -F draft=true"):
            with self.subTest(line=line):
                self.assertTrue(opens_a_pr(line))

    def test_every_copy_of_the_rule_carries_the_same_sentence(self):
        holding = [p for p in shipped_files() if NO_RUN_REFERENCE in p.read_text(encoding="utf-8")]

        drifted = [str(p.relative_to(REPO)) for p in holding
                   if not all(sentence in " ".join(p.read_text(encoding="utf-8").split())
                              for sentence in NO_RUN_REFERENCE_SENTENCES)]

        self.assertTrue(holding)
        self.assertEqual(drifted, [])

    def test_every_file_that_commits_checks_what_it_commits(self):
        literal = {str(p.relative_to(REPO)) for p in shipped_files() if COMMITS.search(p.read_text(encoding="utf-8"))}
        committing = literal | set(COMMITTING_COMMANDS)

        unchecked = sorted(n for n in committing if IDS_CHECK not in (REPO / n).read_text(encoding="utf-8"))

        # A file that commits by a literal `git commit` but is not named is listed, so it is added to the names.
        self.assertEqual(sorted(literal - set(COMMITTING_COMMANDS)), [])
        self.assertEqual(unchecked, [])

    def test_reading_pulls_is_not_opening_one(self):
        lines = ['gh api "repos/$REPO/pulls?state=open&per_page=100"', "gh api -X POST repos/o/r/issues/5/labels"]

        self.assertFalse(opens_a_pr("\n".join(lines)))


if __name__ == "__main__":
    unittest.main()
