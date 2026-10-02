"""A hermetic instance for tests that run `bin/trimtab` or a hook, hermetically.

The package reads its doctrine from an instance root, never from its own
checkout (stage S2 spec, section 3). Tests build one here, in a temporary
directory, so no unit test reads the real `rules/`. The doctrine files have
neutral names so a copy of these tests carries no deployment's filenames; the
prefixes are the ones the fixtures cite (`TST-2`, `NRM-3`).

An instance check is not a unit test: it tests an instance's own content, not
the package, so it reads real doctrine by design. It takes the instance from
`real_instance()` and runs only when that instance is the checkout under test.
When the checkout under test holds no `rules/` (the base's own suite), there is
nothing of its kind to check, and it is skipped with the reason. When the
checkout holds `rules/`, it is itself an instance and must be checked, so
anything other than `TRIMTAB_INSTANCE` naming it is an error: unset (a plain
shell), or another tree (a worktree whose environment names the canonical
checkout). A skip there would pass a stale doctrine. An instance that is given
but invalid is always an error.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from trimtab import roots

ENV = "TRIMTAB_INSTANCE"
REPO_NAME = "owner/repo"
# The checkout these tests live in: the tree an instance check is about. Not
# roots.code_root(), which follows wherever the imported package came from.
CHECKOUT = Path(__file__).resolve().parents[2]


class InstanceCheckRefused(Exception):
    """The checkout under test is an instance, but TRIMTAB_INSTANCE does not name it."""


class InstanceMismatch(InstanceCheckRefused):
    """The checkout under test is an instance, but TRIMTAB_INSTANCE names another tree."""


class InstanceRequired(InstanceCheckRefused):
    """The checkout under test is an instance, but TRIMTAB_INSTANCE is not set."""


def _rule(name: str, prefix: str) -> str:
    return (f'---\nname: {name}\ndescription: "A fixture doctrine file."\nid_prefix: {prefix}\n---\n\n'
            f"# {name}\n\n"
            "## 1. First\n\n- **PREFER** small things.\n\n"
            "## 2. Second\n\n- **AVOID** mocking what you own.\n\n"
            "## 3. Third\n\n- **REQUIRE** a named reason.\n")


DOCTRINE = {"Example.md": _rule("Example", "TST"), "Another.md": _rule("Another", "NRM")}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                           check=True, capture_output=True, text=True).stdout.strip()


def make_instance(parent: Path, *, commit: bool = False) -> Path:
    """An instance under `parent`: doctrine, a lock naming `owner/repo`, optionally one commit."""
    inst = Path(parent) / "instance"
    (inst / "rules").mkdir(parents=True)
    for name, text in DOCTRINE.items():
        (inst / "rules" / name).write_text(text, encoding="utf-8")
    (inst / ".claude").mkdir()
    (inst / ".claude" / "trimtab.json").write_text(json.dumps(
        {"source": REPO_NAME, "trimtab_sha": "0" * 40, "id_prefix": "INS", "schema_version": 1}))
    if commit:
        git(inst, "init", "-q")
        git(inst, "add", "-A")
        git(inst, "commit", "-qm", "instance")
    return inst.resolve()


def add_rule(instance: Path, filename: str, prefix: str) -> None:
    """Add one fixture doctrine file to `instance`, for a test that cites another prefix."""
    name = Path(filename).stem
    (Path(instance) / "rules" / filename).write_text(_rule(name, prefix), encoding="utf-8")


def env_for(instance: Path | None) -> dict[str, str]:
    """This process's environment with the instance set, or with it removed when `instance` is None."""
    env = {k: v for k, v in os.environ.items() if k != ENV}
    if instance is not None:
        env[ENV] = str(instance)
    return env


def real_instance(environ: Mapping[str, str] = os.environ, code: Path | None = None) -> Path:
    """The instance named by TRIMTAB_INSTANCE when it is the checkout under test (`code`).

    An instance check reads an instance's content, not the package's, so it is
    not a unit test and is not held to the unit tests' hermetic rule. It runs
    only where the instance is this checkout (Trimtab's CI, where both are the
    workspace).

    If this checkout holds no `rules/` (trimtab-core, set or unset), there is
    nothing of its own to check, so it skips. If it does hold `rules/`, it is
    an instance and a skip would let a stale HARNESS.md pass, so it raises:
    InstanceRequired when none is set (a plain shell, where bootstrap's
    settings do not reach), InstanceMismatch when another tree is named (a
    Trimtab worktree, whose environment names the canonical checkout).

    A value that is set but does not name an instance raises InstanceInvalid.
    A broken configuration fails loudly, never as a skip.
    """
    code = Path(code or CHECKOUT).resolve()
    is_instance = (code / "rules").is_dir()
    try:
        instance = roots.instance_root(None, environ)
    except roots.InstanceNotSet as err:
        if is_instance:
            raise InstanceRequired(f"the checkout under test {code} is an instance, but {ENV} is "
                                   f"not set; run with {ENV}=\"$PWD\" from {code} to check its "
                                   "doctrine") from err
        raise unittest.SkipTest("instance check, not a unit test: it reads a real instance's "
                                f"content and none is set ({err})") from err
    if instance == code:
        return instance
    if is_instance:
        raise InstanceMismatch(f"the checkout under test {code} is an instance, but {ENV} names "
                               f"{instance}; run with {ENV}=\"$PWD\" from {code} to check its doctrine")
    raise unittest.SkipTest(f"instance check, not a unit test: the instance {instance} is not "
                            f"the checkout under test {code}, which holds no rules/ to check")
