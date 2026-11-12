"""The git and gh the gate runs, hardened against what the gated session can write (ADR 0063 §2).

The gate runs where a session can edit checkouts, set variables and put programs on PATH. So:

- **Binaries** are `git` and `gh` from the system directories only: each a regular file owned by
  root that only root can write, in a directory only root can write. A program a session put on
  PATH is never run.
- **Environment** is built, never inherited: PATH is the system directories, nothing of git's
  steering or config variables survives, and gh keeps only its auth (`GH_CONFIG_DIR`,
  `GH_TOKEN`, `GITHUB_TOKEN`), never a host, a repository or an enterprise endpoint.
- **git** runs with replace objects off, literal pathspecs, planscope's pinned settings (no
  fsmonitor, no commit-graph) and no hooks. A repository with a grafts file or a shallow history
  is refused: grafts still apply with replace objects off, and either can change which commit
  added a file.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from trimtab.planscope import GIT_PINNED

SYSTEM_DIRS = ("/usr/bin", "/bin")
GH_KEEP = ("GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN", "HOME", "LANG", "LC_ALL", "XDG_CONFIG_HOME")
GIT_KEEP = ("HOME", "LANG", "LC_ALL")
GIT_TIMEOUT_SECONDS = 10
GH_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 60


class ToolError(Exception):
    """A trusted binary cannot be found, or a repository is not safe to read."""


def _root_only(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_uid == 0 and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def trusted(name: str, dirs: tuple[str, ...] = SYSTEM_DIRS) -> str:
    """The absolute path of `name` in a system directory, root-owned and writable only by root."""
    for directory in dirs:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        real = os.path.realpath(path)
        if os.access(path, os.X_OK) and _root_only(path) and _root_only(directory) \
                and _root_only(real) and _root_only(os.path.dirname(real)):
            return path
        raise ToolError(f"{path} is not a root-owned binary in a root-only directory")
    raise ToolError(f"no {name} in {', '.join(dirs)}")


def git_env(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Only what git needs and nothing that steers it: built from an allowlist, never by removal."""
    env = {k: environ[k] for k in GIT_KEEP if k in environ}
    # No system or global config: ~/.gitconfig is the session's to write.
    env.update(PATH=":".join(SYSTEM_DIRS), GIT_NO_REPLACE_OBJECTS="1", GIT_LITERAL_PATHSPECS="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


def gh_env(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    env = {k: environ[k] for k in GH_KEEP if k in environ}
    env.update(PATH=":".join(SYSTEM_DIRS), GH_PROMPT_DISABLED="1", NO_COLOR="1")
    return env


class Git:
    """Hardened git reads, checking each repository once for grafts and a shallow history."""

    def __init__(self, binary: str | None = None, environ: Mapping[str, str] = os.environ):
        self._binary = binary or trusted("git")
        self._env = git_env(environ)
        self._checked: set[str] = set()

    def _raw(self, repo: Path, *args: str) -> bytes:
        pins = [a for key, value in GIT_PINNED for a in ("-c", f"{key}={value}")]
        argv = [self._binary, "-C", str(repo), *pins, "-c", "core.hooksPath=/dev/null", *args]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=GIT_TIMEOUT_SECONDS, env=self._env)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ToolError(f"git {args[0]} in {repo.name} failed ({type(err).__name__})") from err
        if done.returncode != 0:
            raise ToolError(f"git {args[0]} in {repo.name} exited {done.returncode}")
        return done.stdout

    def _check(self, repo: Path) -> None:
        key = str(repo.resolve())
        if key in self._checked:
            return
        if self._raw(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise ToolError(f"{repo.name} is a shallow clone, whose history can be cut")
        grafts = Path(self._raw(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/grafts")
                      .decode("utf-8", "replace").strip())
        if os.path.lexists(grafts):
            raise ToolError(f"{repo.name} has a grafts file, which rewrites history")
        self._checked.add(key)

    def fetch(self, repo: Path, branch: str) -> None:
        """Fetch origin's `branch` into its remote-tracking ref, with the user's git config so their
        credentials work. It decides nothing: the caller compares the fetched head with the server's."""
        if not branch or branch.startswith("-") or ".." in branch or any(c in branch for c in " ~^:?*[\\"):
            raise ToolError("not a branch name to fetch")
        env = {k: v for k, v in self._env.items() if k != "GIT_CONFIG_GLOBAL"}
        argv = [self._binary, "-C", str(repo), "-c", "core.hooksPath=/dev/null", "fetch", "--no-tags", "--quiet",
                "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=FETCH_TIMEOUT_SECONDS, env=env)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ToolError(f"git fetch in {repo.name} failed ({type(err).__name__})") from err
        if done.returncode != 0:
            raise ToolError(f"git fetch in {repo.name} exited {done.returncode}")

    def bytes(self, repo: Path, *args: str) -> bytes:
        self._check(repo)
        return self._raw(repo, *args)

    def __call__(self, repo: Path, *args: str) -> str:
        try:
            return self.bytes(repo, *args).decode("utf-8")
        except UnicodeDecodeError as err:
            raise ToolError(f"git {args[0]} in {repo.name} returned text that is not UTF-8") from err


def gh_runner(binary: str | None = None, environ: Mapping[str, str] = os.environ) \
        -> Callable[[list, str | None], subprocess.CompletedProcess]:
    """A `run` for GhApi that calls the trusted gh with the built environment."""
    path = binary or trusted("gh")
    env = gh_env(environ)

    def run(argv: list, stdin: str | None) -> subprocess.CompletedProcess:
        if not argv or argv[0] != "gh":
            raise ToolError("the gate runs only gh")
        return subprocess.run([path, *argv[1:]], input=stdin, capture_output=True, text=True,
                              timeout=GH_TIMEOUT_SECONDS, env=env)

    return run
