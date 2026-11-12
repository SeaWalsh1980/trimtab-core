"""A copy of hooks/ whose PATH reset names a test's stub directory (ADR 0065).

Every shell hook sets `PATH=/usr/bin:/bin` before its first command, so a case
that swaps a tool through PATH cannot do it from outside the hook: the hook
would find the real tool in /usr/bin. Such a case runs this copy instead. Each
.sh differs from the original only in the reset line; everything else, and the
package beside hooks/, is linked. tests/guard-tests.sh has the same helper in
bash (`path_hooks`).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATH_RESET = "export PATH=/usr/bin:/bin"


def hooks_with_path(dest, path):
    """A copy of hooks/ under <dest> whose reset sets PATH to <path>; returns its hooks directory."""
    out = Path(dest) / "hooks"
    out.mkdir(parents=True)
    (Path(dest) / "trimtab").symlink_to(REPO / "trimtab")
    for src in (REPO / "hooks").iterdir():
        if src.suffix != ".sh":
            (out / src.name).symlink_to(src)
            continue
        lines = src.read_text(encoding="utf-8").split("\n")
        if lines.count(PATH_RESET) != 1:
            raise AssertionError(f"{src.name} has {lines.count(PATH_RESET)} PATH resets, not 1")
        swapped = [f"export PATH={path}" if line == PATH_RESET else line for line in lines]
        (out / src.name).write_text("\n".join(swapped), encoding="utf-8")
        (out / src.name).chmod(0o755)
    return out
