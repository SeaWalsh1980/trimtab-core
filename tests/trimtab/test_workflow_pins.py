"""The PyYAML pin is written into two workflows by hand; Dependabot tracks neither."""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
# The heredoc body between `cat > "$req" <<'EOF'` and its closing `EOF`.
REQUIREMENTS = re.compile(r"<<'EOF'\n(.*?)\n\s*EOF\n", re.S)


def requirements(name: str) -> list[str]:
    blocks = REQUIREMENTS.findall((WORKFLOWS / name).read_text(encoding="utf-8"))
    return [" ".join(block.split()) for block in blocks]


class WorkflowPinsTest(unittest.TestCase):
    def test_plan_scope_installs_the_same_pyyaml_as_ci(self):
        ci, plan_scope = requirements("ci.yml"), requirements("plan-scope.yml")

        self.assertEqual(len(ci), 1)
        self.assertEqual(plan_scope, ci)


if __name__ == "__main__":
    unittest.main()
