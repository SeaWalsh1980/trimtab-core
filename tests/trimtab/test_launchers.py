"""The launchers import the base's package, never one in the caller's directory.

Integration tests at the process boundary: each runs a launcher from a
temporary directory holding a decoy `trimtab/` package that announces
itself, and requires the decoy never to run.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]
DECOY = "DECOY-PACKAGE-RAN"


def plant_decoy(root: Path) -> None:
    pkg = root / "trimtab"
    (pkg / "adapters").mkdir(parents=True)
    say = f"import sys\nprint({DECOY!r})\nsys.exit(3)\n"
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(say)
    (pkg / "adapters" / "__init__.py").write_text("")
    for name in ("doctrine_drift", "pr_body_check"):
        (pkg / "adapters" / f"{name}.py").write_text(say)


class Launchers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        plant_decoy(self.cwd)

    def run_from_decoy(self, argv, stdin=""):
        return subprocess.run(argv, cwd=self.cwd, input=stdin, capture_output=True, text=True, timeout=60)

    def test_the_cli_runs_the_base_package_from_a_directory_with_a_decoy(self):
        out = self.run_from_decoy([str(CHECKOUT / "bin" / "trimtab"), "instance", "--help"])
        self.assertNotIn(DECOY, out.stdout + out.stderr)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--json", out.stdout)

    def test_the_drift_hook_runs_the_base_adapter_from_a_directory_with_a_decoy(self):
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(self.cwd)})
        out = self.run_from_decoy([str(CHECKOUT / "hooks" / "doctrine-drift.sh")], payload)
        self.assertNotIn(DECOY, out.stdout + out.stderr)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_pr_body_hook_runs_the_base_adapter_from_a_directory_with_a_decoy(self):
        # `gh pr view` passes the hook's `gh pr` gate, so Python runs, and the
        # real adapter allows it: it checks bodies for `create` and `edit` only.
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr view 1"}, "cwd": str(self.cwd)})
        out = self.run_from_decoy([str(CHECKOUT / "hooks" / "pr-body-check.sh")], payload)
        self.assertNotIn(DECOY, out.stdout + out.stderr)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_pr_body_hook_allows_when_the_interpreter_rejects_p(self):
        # An interpreter older than 3.11 exits 2 on the unknown -P option, the
        # code the adapter uses for BLOCK. The hook fails open, so it must exit 0.
        fakebin = self.cwd / "fakebin"
        fakebin.mkdir()
        fake = fakebin / "python3"
        fake.write_text(
            '#!/bin/sh\nfor a in "$@"; do\n'
            '  [ "$a" = "-P" ] && { echo "Unknown option: -P" >&2; exit 2; }\n'
            "done\nexit 0\n"
        )
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr create --draft"}, "cwd": str(self.cwd)})
        out = subprocess.run([str(CHECKOUT / "hooks" / "pr-body-check.sh")], cwd=self.cwd, input=payload,
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
