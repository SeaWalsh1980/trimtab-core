"""The PyYAML pin is written into two workflows by hand; Dependabot tracks neither.

The parser is unit-tested on in-memory text. The comparison of the two
workflows is a repository-consistency check, as test_doc_references is: it
reads tracked files on purpose, because the files are what it checks.
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
# The heredoc body between `cat > "$req" <<'EOF'` and its closing `EOF`.
REQUIREMENTS = re.compile(r"<<'EOF'\n(.*?)\n\s*EOF\n", re.S)

PIN = "pyyaml==6.0.3 \\\n    --hash=sha256:aaaa"
WORKFLOW_TEXT = f"run: |\n  cat > \"$req\" <<'EOF'\n  {PIN}\n  EOF\n  pip install -r \"$req\"\n"


def requirements_of(text: str) -> list[str]:
    return [" ".join(block.split()) for block in REQUIREMENTS.findall(text)]


def workflow_requirements(name: str) -> list[str]:
    return requirements_of((WORKFLOWS / name).read_text(encoding="utf-8"))


class RequirementsParserTest(unittest.TestCase):
    def test_reads_the_heredoc_body_ignoring_indentation(self):
        self.assertEqual(requirements_of(WORKFLOW_TEXT), ["pyyaml==6.0.3 \\ --hash=sha256:aaaa"])

    def test_a_changed_hash_reads_differently(self):
        changed = WORKFLOW_TEXT.replace("aaaa", "bbbb")

        self.assertNotEqual(requirements_of(changed), requirements_of(WORKFLOW_TEXT))


class WorkflowPinsConsistencyTest(unittest.TestCase):
    def test_plan_scope_installs_the_same_pyyaml_as_ci(self):
        ci, plan_scope = workflow_requirements("ci.yml"), workflow_requirements("plan-scope.yml")

        self.assertEqual(len(ci), 1)
        self.assertEqual(plan_scope, ci)


if __name__ == "__main__":
    unittest.main()
