"""The shipped commands, agents and templates reach GitHub through REST only.

gh's porcelain goes through GraphQL, which Claude Code cloud sessions refuse:
a command that used it could not run in a routine. Reads this checkout's own
files; writes nothing.
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


def shipped_files():
    for top in SHIPPED:
        root = REPO / top
        if root.is_dir():
            yield from sorted(p for p in root.rglob("*") if p.is_file())


def graphql_lines(text):
    return [n for n, line in enumerate(text.splitlines(), 1) if GRAPHQL.search(line)]


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
        lines = ["gh api 'repos/{owner}/{repo}' --jq .full_name", "gh api -X GET search/issues -f q=x",
                 "gh api -X POST repos/o/r/pulls -F body=@f"]

        self.assertEqual(graphql_lines("\n".join(lines)), [])


if __name__ == "__main__":
    unittest.main()
