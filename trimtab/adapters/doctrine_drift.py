"""SessionStart adapter: warn when the instance's doctrine differs from the project's lock.

Never blocks and always exits 0 (ADR 0005). Silent when the project has
not adopted Trimtab, when the lock matches or only non-doctrine files changed
since it (ADR 0008), and in the instance's own checkouts (where the lock
is a baseline, not a pin). Otherwise prints one JSON object whose
`systemMessage` the operator sees, including when TRIMTAB_INSTANCE is unset:
this is a policy hook, so it fails open, but never silently.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from trimtab import config as project_config
from trimtab import roots

GIT_TIMEOUT_SECONDS = 5


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _common_dir(root: Path) -> Path | None:
    out = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out).resolve() if out else None


def warnings(cwd: Path) -> list[str]:
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return []
    project = Path(top)
    path = project / project_config.PATH
    if not path.is_file():
        return []
    try:
        instance = roots.instance_root()
    except roots.InstanceError:
        return ["TRIMTAB_INSTANCE is not set, so the lock cannot be checked; re-run bootstrap.sh."]
    if _common_dir(project) is not None and _common_dir(project) == _common_dir(instance):
        return []  # the instance itself
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"{project_config.PATH} is unreadable or not valid JSON; the Trimtab lock cannot be checked."]
    if not isinstance(data, dict):
        return [f"{project_config.PATH} is not a JSON object; the Trimtab lock cannot be checked."]
    out = []
    locked, installed = data.get("trimtab_sha"), _git(instance, "rev-parse", "HEAD")
    if not isinstance(locked, str) or not locked:
        out.append(f"{project_config.PATH} has no trimtab_sha, so this project has not recorded which "
                   "Trimtab it was reviewed against.")
    elif installed is None:
        out.append("the instance checkout's SHA could not be read, so the lock cannot be checked.")
    elif installed != locked:
        if not project_config.is_commit_sha(locked):
            out.append(f"{project_config.PATH} has a trimtab_sha that is not a commit name.")
        else:
            try:
                code = subprocess.run(["git", "-C", str(instance), "diff", "--quiet", "--end-of-options",
                                       locked, "HEAD", "--", "rules", "HARNESS.md"],
                                      capture_output=True, timeout=GIT_TIMEOUT_SECONDS).returncode
            except (OSError, subprocess.TimeoutExpired):
                code = None
            if code == 1:
                out.append(f"this project was reviewed against instance {locked[:12]}; the installed "
                           f"instance's doctrine has changed since ({installed[:12]}). Review the changed "
                           "items and move the lock (/trimtab-bump).")
            elif code != 0:
                out.append(f"the instance cannot compare its doctrine at {locked[:12]} (unknown commit?).")
    version = data.get("schema_version")
    if isinstance(version, int) and version < project_config.SCHEMA_VERSION:
        out.append(f"{project_config.PATH} schema_version {version} is behind {project_config.SCHEMA_VERSION}; "
                   "re-render the scaffold (trimtab adopt --update).")
    return out


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        cwd = Path(payload.get("cwd") or ".") if isinstance(payload, dict) else Path(".")
        found = warnings(cwd)
    except Exception as err:  # SessionStart cannot block; say so rather than crash
        print(f"doctrine-drift: internal error ({type(err).__name__}); lock not checked", file=sys.stderr)
        return 0
    if found:
        print(json.dumps({"systemMessage": "Trimtab drift: " + " ".join(found)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
