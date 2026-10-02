"""real_instance(): when an instance check runs, skips, or fails (stage S2 pre-extraction prep).

No test reads the real doctrine: each builds any instance in a temporary
directory. All but one pass their own environment mapping. The exception,
`test_the_checkout_under_test_is_the_tests_tree_not_the_imported_packages`,
runs a child Python with a copy of the package, in the parent's environment
plus its own settings. It also branches on the real checkout's layout by
design: InstanceMismatch where the checkout holds `rules/`, a skip where it
does not (trimtab-core). Both outcomes pass.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from instance_fixture import (CHECKOUT, ENV, InstanceCheckRefused, InstanceMismatch,
                              InstanceRequired, env_for, make_instance, real_instance)
from trimtab import roots

# Run in a child whose imported `trimtab` is a scratch copy, so the package's
# checkout (roots.code_root()) and the tests' tree (CHECKOUT) really differ.
PROBE = """
import unittest
from trimtab import roots
from instance_fixture import InstanceCheckRefused, real_instance
print("code_root:", roots.code_root())
try:
    real_instance()
    print("outcome: returned")
except (unittest.SkipTest, InstanceCheckRefused) as err:
    print("outcome:", type(err).__name__, err)
"""


def raised(environ, code=None):
    """The exception real_instance() raises for `environ` and `code`, or None.

    Caught here rather than with assertRaises: a SkipTest escaping a test makes
    unittest skip it, which would hide a regression from skip to error.
    """
    try:
        real_instance(environ, code)
    except (unittest.SkipTest, roots.InstanceError, InstanceCheckRefused) as err:
        return err
    return None


class RealInstance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_checkout_under_test_named_as_the_instance_is_returned(self):
        inst = make_instance(self.parent)

        found = real_instance({ENV: str(inst)}, code=inst)

        self.assertEqual(found, inst)

    def test_another_tree_named_while_the_checkout_holds_no_rules_skips_naming_both(self):
        inst = make_instance(self.parent)
        checkout = self.parent / "checkout"
        checkout.mkdir()

        err = raised({ENV: str(inst)}, code=checkout)

        self.assertIsInstance(err, unittest.SkipTest)
        self.assertIn(str(inst), str(err))
        self.assertIn(str(checkout.resolve()), str(err))

    def test_another_tree_named_while_the_checkout_is_an_instance_is_an_error(self):
        inst = make_instance(self.parent)
        checkout = self.parent / "checkout"
        (checkout / "rules").mkdir(parents=True)

        err = raised({ENV: str(inst)}, code=checkout)

        self.assertIsInstance(err, InstanceMismatch)
        self.assertIn(f'{ENV}="$PWD"', str(err))

    def test_the_checkout_under_test_is_the_tests_tree_not_the_imported_packages(self):
        # Another checkout that is an instance and holds the imported package: if the
        # default followed the package, it would match the instance and run against it.
        other = make_instance(self.parent)
        shutil.copytree(CHECKOUT / "trimtab", other / "trimtab",
                        ignore=shutil.ignore_patterns("__pycache__"))
        env = {**env_for(other), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": os.pathsep.join([str(other), str(Path(__file__).resolve().parent)])}

        done = subprocess.run([sys.executable, "-c", PROBE], cwd=other, env=env,
                              capture_output=True, text=True)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"code_root: {other}\n", done.stdout)
        self.assertNotIn("outcome: returned", done.stdout)
        self.assertIn(str(CHECKOUT), done.stdout)

    def test_no_instance_set_while_the_checkout_holds_no_rules_skips_and_says_why(self):
        checkout = self.parent / "checkout"
        checkout.mkdir()

        err = raised({}, code=checkout)

        self.assertIsInstance(err, unittest.SkipTest)
        self.assertIn("not a unit test", str(err))

    def test_an_empty_value_while_the_checkout_holds_no_rules_skips(self):
        checkout = self.parent / "checkout"
        checkout.mkdir()

        err = raised({ENV: ""}, code=checkout)

        self.assertIsInstance(err, unittest.SkipTest)

    def test_no_instance_set_while_the_checkout_is_an_instance_is_an_error(self):
        checkout = self.parent / "checkout"
        (checkout / "rules").mkdir(parents=True)

        err = raised({}, code=checkout)

        self.assertIsInstance(err, InstanceRequired)
        self.assertIn(f'{ENV}="$PWD"', str(err))

    def test_an_empty_value_while_the_checkout_is_an_instance_is_an_error(self):
        checkout = self.parent / "checkout"
        (checkout / "rules").mkdir(parents=True)

        err = raised({ENV: ""}, code=checkout)

        self.assertIsInstance(err, InstanceRequired)

    def test_a_missing_path_is_an_error_not_a_skip(self):
        err = raised({ENV: str(self.parent / "missing")})

        self.assertIsInstance(err, roots.InstanceInvalid)

    def test_a_directory_without_rules_is_an_error_not_a_skip(self):
        bare = self.parent / "bare"
        bare.mkdir()

        err = raised({ENV: str(bare)})

        self.assertIsInstance(err, roots.InstanceInvalid)


if __name__ == "__main__":
    unittest.main()
