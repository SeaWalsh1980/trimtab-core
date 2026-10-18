"""The launchers import the base's package, never one in the caller's directory.

Integration tests at the process boundary: each runs a launcher from a
temporary directory holding a decoy `trimtab/` package that announces
itself, and requires the decoy never to run.
"""

import json
import os
import shutil
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
    for name in ("doctrine_drift", "pr_body_check", "pr_monitor"):
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

    def test_the_pr_monitor_recorder_runs_the_base_adapter_from_a_directory_with_a_decoy(self):
        # `gh pr create` passes the recorder's gate, so Python runs; the empty stdout is not
        # a PR URL, so the real adapter records nothing and exits 0. The decoy would exit 3.
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"},
                              "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
                              "cwd": str(self.cwd)})
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.cwd / "config")}
        out = subprocess.run([str(CHECKOUT / "hooks" / "pr-monitor-record.sh")], cwd=self.cwd, input=payload,
                             env=env, capture_output=True, text=True, timeout=60)
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


class NoBytecode(unittest.TestCase):
    """Integration test at the process boundary: a launcher run leaves no
    bytecode beside the package it ran (a snapshot is never written)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = Path(self.tmp.name) / "base"
        for d in ("trimtab", "bin", "hooks"):
            shutil.copytree(CHECKOUT / d, self.tree / d,
                            ignore=shutil.ignore_patterns("__pycache__"))
        # A caller's environment that already suppresses or redirects bytecode
        # would let these pass without the launcher doing anything.
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}

    def run_launcher(self, argv, stdin=""):
        subprocess.run(argv, cwd=self.tmp.name, input=stdin, env=self.env,
                       capture_output=True, text=True, timeout=60)

    def caches(self):
        return sorted(str(p.relative_to(self.tree)) for p in self.tree.rglob("__pycache__"))

    def test_the_cli_writes_no_bytecode(self):
        self.run_launcher([str(self.tree / "bin" / "trimtab"), "--help"])

        self.assertEqual(self.caches(), [])

    def test_the_drift_hook_writes_no_bytecode(self):
        self.run_launcher(["bash", str(self.tree / "hooks" / "doctrine-drift.sh")], "{}")

        self.assertEqual(self.caches(), [])

    def test_the_pr_body_hook_writes_no_bytecode(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr create --body x"}})

        self.run_launcher(["bash", str(self.tree / "hooks" / "pr-body-check.sh")], payload)

        self.assertEqual(self.caches(), [])

    def test_the_pr_monitor_recorder_writes_no_bytecode(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"},
                              "tool_response": {"stdout": ""}})

        self.run_launcher(["bash", str(self.tree / "hooks" / "pr-monitor-record.sh")], payload)

        self.assertEqual(self.caches(), [])


def plant(path: Path, mark: Path) -> None:
    """A module that, if anything imports it, leaves `mark` behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"open({str(mark)!r}, 'w').close()\n")


class LaunchersRunIsolated(unittest.TestCase):
    """The launchers in bin/ run python3 isolated, as the hooks do (ADR 0035).

    Each case copies bin/, hooks/ and trimtab/ to a temporary root, plants code
    where the old launchers would have run it, runs the launcher, and requires
    the plant's mark to be absent.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "root"
        for name in ("bin", "hooks", "trimtab"):
            shutil.copytree(CHECKOUT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.mark = self.base / "PLANT-RAN"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        self.env.update({"PYTHONDONTWRITEBYTECODE": "1", "HOME": str(self.base / "home"),
                         "CLAUDE_RULE_USAGE_DIR": str(self.base / "logs"),
                         "CLAUDE_RULE_USAGE_LOG": str(self.base / "usage.jsonl")})
        (self.base / "usage.jsonl").write_text("")

    def user_site(self) -> Path:
        """The user site-packages directory a PYTHONUSERBASE under the temp dir gives."""
        userbase = self.base / "userbase"
        out = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                             env={**self.env, "PYTHONUSERBASE": str(userbase)},
                             capture_output=True, text=True, check=True)
        self.env["PYTHONUSERBASE"] = str(userbase)
        return Path(out.stdout.strip())

    def cli(self):
        return subprocess.run([str(self.root / "bin" / "trimtab"), "--help"], cwd=self.base, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def weekly(self):
        return subprocess.run(["bash", str(self.root / "bin" / "rule-usage-weekly.sh")], cwd=self.base,
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_the_cli_does_not_import_a_standard_library_name_planted_at_the_root(self):
        plant(self.root / "json.py", self.mark)

        out = self.cli()

        self.assertFalse(self.mark.exists())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_cli_does_not_run_a_sitecustomize_planted_at_the_root(self):
        plant(self.root / "sitecustomize.py", self.mark)

        out = self.cli()

        self.assertFalse(self.mark.exists())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_cli_does_not_run_a_pth_file_in_user_site_packages(self):
        site_dir = self.user_site()
        site_dir.mkdir(parents=True)
        (site_dir / "plant.pth").write_text(f"import os; open({str(self.mark)!r}, 'w').close()\n")

        out = self.cli()

        self.assertFalse(self.mark.exists())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_cli_ignores_a_pythonpath_the_caller_set(self):
        plant(self.base / "pp" / "json.py", self.mark)
        self.env["PYTHONPATH"] = str(self.base / "pp")

        out = self.cli()

        self.assertFalse(self.mark.exists())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_weekly_report_does_not_import_a_name_planted_beside_its_script(self):
        plant(self.root / "bin" / "json.py", self.mark)

        out = self.weekly()

        self.assertFalse(self.mark.exists())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_weekly_report_does_not_run_a_pth_file_in_user_site_packages(self):
        site_dir = self.user_site()
        site_dir.mkdir(parents=True)
        (site_dir / "plant.pth").write_text(f"import os; open({str(self.mark)!r}, 'w').close()\n")

        out = self.weekly()

        self.assertFalse(self.mark.exists())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_weekly_report_still_writes_its_report(self):
        out = self.weekly()

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(list((self.base / "logs").glob("rule-usage-*.md")), out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
