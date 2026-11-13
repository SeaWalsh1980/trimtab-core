"""claude-review's MEDIUM on PR 166 (9e10bd5): a push built only from command substitutions, each case failing
without the fix. GitHub and git are injected (TST-2)."""

import unittest

from test_guard_github_plan import ON_MAIN, NoFacts
from test_guard_github_plan_bypasses import Cases
from test_planrows import BRANCH


class CommandWordsBuiltFromSubstitutions(Cases):
    def test_a_command_word_assembled_from_a_substitution_and_letters_refuses(self):
        for cmd in (f"$(printf gi)t pu$(echo s)h origin HEAD:{BRANCH}", "$(printf gi)t pu$(echo s)h origin HEAD",
                    "g$(echo i)t status", "`printf gi`t status", f"ls; $(echo gi)t pu$(echo s)h origin {BRANCH}",
                    "x=1 && g`echo i`t status"):
            with self.subTest(cmd):
                self.refused(cmd, "assembled from a substitution", git=ON_MAIN, facts=NoFacts())

    def test_ordinary_substitutions_pass(self):
        for cmd in ('$(dirname "$0")/run.sh', 'echo "$(date)"', "touch file-$(date).log", "ls $(pwd)",
                    'git commit -m "$(cat msg)"', 'x=$(git rev-parse HEAD); git push origin feature/x',
                    "VAR=$(cmd) tool", "echo `date`"):
            with self.subTest(cmd):
                self.allowed(cmd, git=ON_MAIN, facts=NoFacts())


if __name__ == "__main__":
    unittest.main()
