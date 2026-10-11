"""Contracts between the session files (agents, commands) and the parser.

The planner's output is copied into PR bodies and parsed by `check-pr`, so the
shape its definition tells it to emit must parse against the live registry. A
second citation shape that nothing parsed is how an earlier project lost data.
"""

import re
import tempfile
import unittest
from pathlib import Path

from instance_fixture import add_rule, make_instance, real_instance
from trimtab import routines
from trimtab.capture.prblock import check_body
from trimtab.registry.lookup import registry_for
from trimtab.registry.rules import frontmatter

REPO = Path(__file__).resolve().parents[2]
CONTRACT = re.compile(r"<!-- contract: (\S+) -->\n````markdown\n(.*?)\n````", re.S)
WRITING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.S)
DOCTRINE_READERS = ("trimtab-planner", "trimtab-reviewer", "trimtab-silent-failure-hunter",
                    "trimtab-test-analyzer")
# The whole snippet: the installed CLI, never the tree under review's, and the doctrine root
# from `trimtab instance --root` and nowhere else (ADR 0008).
DOCTRINE_SNIPPET = (
    'TRIMTAB="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bin/trimtab"',
    'DOCTRINE=$("$TRIMTAB" instance --root) && echo "ok: doctrine at $DOCTRINE"',
)


def contracts(path: Path) -> dict[str, str]:
    return dict(CONTRACT.findall(path.read_text(encoding="utf-8")))


def doctrine_snippet(path: Path) -> tuple[str, ...] | None:
    """The bash block that resolves the doctrine root, one stripped line per entry, or None."""
    for block in BASH_BLOCK.findall(path.read_text(encoding="utf-8")):
        if "DOCTRINE=" in block:
            return tuple(line.strip() for line in block.splitlines() if line.strip())
    return None


class PlannerOutput(unittest.TestCase):
    """The planner's documented output parses with `check-pr`.

    Against a fixture registry holding the example's prefixes always, so the
    base's suite pins the contract too; against the instance's registry as an
    instance check, which skips unless the instance is the checkout under test.
    """

    def test_the_planners_documented_output_parses_against_a_fixture_registry(self):
        example = contracts(REPO / "agents" / "trimtab-planner.md")["planner-output"]
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))
            add_rule(inst, "Engineering.md", "ENG")  # the example cites TST-2 and ENG-2
            registry, problems = registry_for(inst)
        self.assertEqual(problems, [])

        result = check_body(example + "\n\n## Process cost\n```yaml\n```\n", registry)

        self.assertEqual(result.problems, ())
        self.assertTrue(result.block.applied and result.block.feedback)

    def test_the_planners_documented_output_parses_against_the_live_registry(self):
        registry, problems = registry_for(real_instance())
        self.assertEqual(problems, [])
        example = contracts(REPO / "agents" / "trimtab-planner.md")["planner-output"]

        result = check_body(example + "\n\n## Process cost\n```yaml\n```\n", registry)

        self.assertEqual(result.problems, ())
        self.assertTrue(result.block.applied and result.block.feedback)


class Agents(unittest.TestCase):
    def test_every_agent_is_named_after_its_file_and_has_no_file_editing_tool(self):
        # This removes the editing tools only. Bash is still granted unscoped, so
        # "never edits" rests on the agent's instructions and the guards, not on
        # this test. Scoping the agents' Bash is an open gap.
        agents = sorted((REPO / "agents").glob("*.md"))
        self.assertGreaterEqual(len(agents), 4)
        for path in agents:
            with self.subTest(agent=path.name):
                meta = frontmatter(path.read_text(encoding="utf-8")) or {}
                tools = {t.strip() for t in str(meta.get("tools", "")).split(",")}

                self.assertEqual(meta.get("name"), path.stem)
                self.assertTrue(meta.get("description"))
                self.assertFalse(tools & WRITING_TOOLS, "planning and review agents never edit")

    def test_every_command_has_a_description(self):
        for path in sorted((REPO / "commands").glob("*.md")):
            with self.subTest(command=path.name):
                self.assertTrue((frontmatter(path.read_text(encoding="utf-8")) or {}).get("description"))


class DoctrineRoot(unittest.TestCase):
    """The agents take the doctrine root from `trimtab instance`, never the code checkout (ADR 0008)."""

    def test_the_doctrine_reading_agents_share_one_snippet_that_asks_trimtab_instance(self):
        snippets = {name: doctrine_snippet(REPO / "agents" / f"{name}.md") for name in DOCTRINE_READERS}

        self.assertNotIn(None, snippets.values(), snippets)
        self.assertEqual(len(set(snippets.values())), 1, snippets)
        self.assertEqual(snippets[DOCTRINE_READERS[0]], DOCTRINE_SNIPPET)

    def test_each_doctrine_reading_agent_forbids_the_code_root_fallback(self):
        for name in DOCTRINE_READERS:
            with self.subTest(agent=name):
                text = " ".join((REPO / "agents" / f"{name}.md").read_text(encoding="utf-8").split())

                self.assertIn("never read `rules/`", text.lower())

    def test_no_agent_derives_the_doctrine_root_from_the_code_checkout(self):
        for path in sorted((REPO / "agents").glob("*.md")):
            with self.subTest(agent=path.name):
                text = path.read_text(encoding="utf-8")

                self.assertNotIn("readlink -f", text)
                self.assertNotIn("TRIMTAB_ROOT", text)


class Routines(unittest.TestCase):
    """A loop routine states its connectors, and is created disabled until a run is read."""

    def test_the_upstream_retro_spec_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = make_instance(Path(tmp))
            spec = routines.render("upstream-retro", routines.values(inst, {"environment_id": "placeholder"}))

        self.assertEqual(spec["mcp_connections"], [])
        self.assertIs(spec["enabled"], False)
        self.assertIn("/trimtab-upstream-retro", spec["job_config"]["ccr"]["events"][0]["data"]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
