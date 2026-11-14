"""A copy of hooks/ whose PATH reset names a test's stub directory (ADR 0067).

Every shell hook sets `PATH=/usr/bin:/bin` before its first command, so a case
that swaps a tool through PATH cannot do it from outside the hook: the hook
would find the real tool in /usr/bin. Such a case runs this copy instead. Each
.sh differs from the original only in the reset line; everything else, and the
package beside hooks/, is linked. tests/guard-tests.sh has the same helper in
bash (`path_hooks`).
"""

import os
import shlex
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATH_RESET = "export PATH=/usr/bin:/bin"
# session-mode.sh sets PATH again from this variable when it runs as the hook,
# so a copy that names a stub PATH has to change it too.
SYSTEM_PATH_LITERAL = "SM_SYSTEM_PATH=/usr/bin:/bin"


OVERRIDE_PREFIX = "HOOK_ALLOW_"


def without_hook_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """<environ> (default: this process's) minus every HOOK_ALLOW_* key.

    The operator exports those overrides before launching claude (rules/ConfigInspection.md),
    so they are in the shell that runs the suite. A hook a test spawns with that environment
    allows what the test expects it to block. Read os.environ at call time, so a test that
    patches it is honoured. A case that wants an override sets it after this call.
    """
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if not k.startswith(OVERRIDE_PREFIX)}


class NoCanonicalReset(AssertionError):
    """A hook without exactly one canonical reset line: the copy would run an unknown hook."""


def swap_reset(text, path):
    """<text> with its one canonical reset replaced by one that sets PATH to <path>, quoted."""
    lines = text.split("\n")
    count = lines.count(PATH_RESET)
    if count != 1:
        raise NoCanonicalReset(f"{count} canonical PATH resets, not 1")
    swapped = []
    for line in lines:
        if line == PATH_RESET:
            line = f"export PATH={shlex.quote(path)}"
        elif line == SYSTEM_PATH_LITERAL:
            line = f"SM_SYSTEM_PATH={shlex.quote(path)}"
        swapped.append(line)
    return "\n".join(swapped)


def hooks_with_path(dest, path, hooks=REPO / "hooks"):
    """A copy of <hooks> under <dest> whose reset sets PATH to <path>; returns its hooks directory."""
    out = Path(dest) / "hooks"
    out.mkdir(parents=True)
    package = hooks.parent / "trimtab"
    if not package.is_dir():
        raise FileNotFoundError(f"no trimtab/ beside {hooks}: every adapter in the copy would fail to load")
    (Path(dest) / "trimtab").symlink_to(package)
    for src in hooks.iterdir():
        if src.suffix != ".sh":
            (out / src.name).symlink_to(src)
            continue
        try:
            swapped = swap_reset(src.read_text(encoding="utf-8"), path)
        except NoCanonicalReset as fault:
            raise NoCanonicalReset(f"{src.name}: {fault}") from None
        (out / src.name).write_text(swapped, encoding="utf-8")
        (out / src.name).chmod(0o755)
    return out
