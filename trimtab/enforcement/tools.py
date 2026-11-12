"""The git and gh the gate runs, hardened against what the gated session can write (ADR 0063 §2).

The gate runs where a session can edit checkouts, git config and object files, set variables
and put programs on PATH. So:

- **Binaries** are `git` and `gh` from the system directories only: each a regular file owned by
  root that only root can write, in a directory only root can write. A program a session put on
  PATH is never run.
- **Environment** is built, never inherited: PATH is the system directories, nothing of git's
  steering or config variables survives, no global or system git config is read, and gh keeps only
  its auth (`GH_CONFIG_DIR`, `GH_TOKEN`, `GITHUB_TOKEN`). gh's own config is refused when it names
  a unix socket or any host but github.com.
- **Objects are verified, not trusted.** Every commit, tree and blob a decision rests on is read
  through one `git cat-file --batch` and its SHA-1 recomputed here, so a substituted object file,
  a replace ref or a graft cannot change what a SHA reads as. History is walked here, from
  verified commits, never by `git log`. A repository with alternates or a shared common dir is
  refused, since its objects live outside it.
- **The fetch** goes to an explicit `https://github.com/<owner>/<name>.git`, into a fresh bare
  repository under a private temporary directory, with no config but what is pinned here, only the
  https protocol, and gh as the one credential helper. Nothing a session wrote to any git config
  takes part.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from trimtab.planscope import GIT_PINNED

SYSTEM_DIRS = ("/usr/bin", "/bin")
GH_KEEP = ("GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN", "HOME", "LANG", "LC_ALL", "XDG_CONFIG_HOME")
GIT_KEEP = ("HOME", "LANG", "LC_ALL")
GIT_TIMEOUT_SECONDS = 10
GH_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 60
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
WALK_LIMIT = 20000  # first-parent commits a history walk reads before it refuses
STDERR_CHARS = 200


class ToolError(Exception):
    """A trusted binary cannot be found, a repository is not safe to read, or an object is not what its SHA says."""


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


def gh_config_problem(environ: Mapping[str, str] = os.environ) -> str | None:
    """Why gh's own config could redirect its reads, or None: a unix socket, or a host other than github.com."""
    if environ.get("GH_CONFIG_DIR"):
        directory = Path(environ["GH_CONFIG_DIR"])
    elif environ.get("XDG_CONFIG_HOME"):
        directory = Path(environ["XDG_CONFIG_HOME"]) / "gh"
    else:
        directory = Path(environ.get("HOME", "/nonexistent")) / ".config" / "gh"
    for name in ("config.yml", "hosts.yml"):
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            return f"gh's {name} cannot be read"
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "http_unix_socket" and value.strip() not in ("", '""', "''"):
                return f"gh's {name} sets http_unix_socket"
            if name == "hosts.yml" and line[:1] not in ("", " ", "\t", "#") and key.strip() != "github.com":
                return "gh's hosts.yml names a host other than github.com"
    return None


def _stderr(done: subprocess.CompletedProcess) -> str:
    text = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
    text = " ".join(text.split())
    return f": {text[:STDERR_CHARS]}" if text else ""


class Git:
    """Hardened git, checking each repository once for alternates, a shared common dir, grafts and shallowness."""

    def __init__(self, binary: str | None = None, environ: Mapping[str, str] = os.environ):
        self._binary = binary or trusted("git")
        self._environ = dict(environ)
        self._env = git_env(environ)
        self._checked: set[str] = set()

    @property
    def binary(self) -> str:
        return self._binary

    def _pins(self) -> list[str]:
        return [a for key, value in (*GIT_PINNED, ("core.hooksPath", os.devnull)) for a in ("-c", f"{key}={value}")]

    def _raw(self, repo: Path, *args: str, stdin: bytes | None = None, env: dict | None = None,
             timeout: float = GIT_TIMEOUT_SECONDS) -> bytes:
        argv = [self._binary, "-C", str(repo), *self._pins(), *args]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=timeout, env=env or self._env, input=stdin)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ToolError(f"git {args[0]} in {repo.name} failed ({type(err).__name__})") from err
        if done.returncode != 0:
            raise ToolError(f"git {args[0]} in {repo.name} exited {done.returncode}{_stderr(done)}")
        return done.stdout

    def _path(self, repo: Path, name: str) -> Path:
        return Path(self._raw(repo, "rev-parse", "--path-format=absolute", "--git-path", name)
                    .decode("utf-8", "replace").strip())

    def check(self, repo: Path, allow_shallow: bool = False) -> None:
        key = str(repo.resolve())
        if key in self._checked:
            return
        if not allow_shallow and self._raw(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise ToolError(f"{repo.name} is a shallow clone, whose history can be cut")
        for name, why in (("info/grafts", "a grafts file, which rewrites history"),
                          ("objects/info/alternates", "alternates, whose objects live elsewhere"),
                          ("commondir", "a shared common dir, whose objects live elsewhere")):
            if os.path.lexists(self._path(repo, name)):
                raise ToolError(f"{repo.name} has {why}")
        self._checked.add(key)

    def bytes(self, repo: Path, *args: str) -> bytes:
        self.check(repo)
        return self._raw(repo, *args)

    def __call__(self, repo: Path, *args: str) -> str:
        try:
            return self.bytes(repo, *args).decode("utf-8")
        except UnicodeDecodeError as err:
            raise ToolError(f"git {args[0]} in {repo.name} returned text that is not UTF-8") from err

    def objects(self, repo: Path, allow_shallow: bool = False) -> "Objects":
        self.check(repo, allow_shallow=allow_shallow)
        return Objects(self, repo)

    def batch(self, repo: Path, shas: list[str]) -> bytes:
        stdin = "".join(f"{s}\n" for s in shas).encode("ascii")
        return self._raw(repo, "cat-file", "--batch", stdin=stdin)

    def fetch_clean(self, repository: str, branch: str, into: Path) -> str:
        """Fetch `branch` of github.com/`repository` into a new bare repository at `into`, depth 1, with
        nothing but pinned config. Returns the fetched tip. The caller compares it with the server's."""
        if not REPOSITORY.fullmatch(repository) or ".." in repository:
            raise ToolError("not owner/name to fetch")
        if not BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".", "/", ".lock")):
            raise ToolError("not a branch name to fetch")
        gh = trusted("gh")
        env = {**self._env, **{k: self._environ[k] for k in ("GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN",
                                                              "XDG_CONFIG_HOME") if k in self._environ}}
        self._raw(into.parent, "init", "-q", "--bare", str(into), env=env)
        fetch_pins = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.sshCommand=",
                      "-c", "credential.helper=", "-c", f"credential.helper=!{gh} auth git-credential",
                      "-c", "http.sslVerify=true", "-c", "fetch.fsckObjects=true"]
        url = f"https://github.com/{repository}.git"
        self._raw(into, *fetch_pins, "fetch", "--no-tags", "--quiet", "--depth=1", url,
                  f"+refs/heads/{branch}:refs/heads/{branch}", env=env, timeout=FETCH_TIMEOUT_SECONDS)
        tip = self._raw(into, "rev-parse", "--verify", "--end-of-options", f"refs/heads/{branch}^{{commit}}",
                        env=env).decode("ascii", "replace").strip()
        if not SHA.fullmatch(tip):
            raise ToolError("the fetched tip is not a commit")
        return tip


class Objects:
    """Objects read by SHA and re-hashed here; history walked from verified commits only."""

    def __init__(self, git: Git, repo: Path):
        self._git = git
        self._repo = repo
        self._cache: dict[str, tuple[str, bytes]] = {}

    def get_many(self, shas: list[str]) -> None:
        todo = [s for s in dict.fromkeys(shas) if s not in self._cache]
        if not todo:
            return
        if not all(SHA.fullmatch(s) for s in todo):
            raise ToolError("an object name is not a full SHA-1")
        out = self._git.batch(self._repo, todo)
        pos = 0
        for sha in todo:
            end = out.find(b"\n", pos)
            header = out[pos:end].decode("ascii", "replace").split(" ")
            if len(header) != 3 or header[0] != sha:
                raise ToolError(f"{self._repo.name} has no object {sha[:12]}")
            kind, size = header[1], int(header[2])
            body = out[end + 1:end + 1 + size]
            if len(body) != size or out[end + 1 + size:end + 2 + size] != b"\n":
                raise ToolError(f"{self._repo.name}'s object {sha[:12]} was cut short")
            if hashlib.sha1(f"{kind} {size}\0".encode("ascii") + body).hexdigest() != sha:
                raise ToolError(f"{self._repo.name}'s object {sha[:12]} does not hash to its name")
            self._cache[sha] = (kind, body)
            pos = end + 2 + size

    def get(self, sha: str, kind: str) -> bytes:
        self.get_many([sha])
        found, body = self._cache[sha]
        if found != kind:
            raise ToolError(f"{sha[:12]} is a {found}, not a {kind}")
        return body

    def commit(self, sha: str) -> tuple[str, tuple[str, ...]]:
        """(tree, parents) of a commit."""
        tree, parents = None, []
        for line in self.get(sha, "commit").split(b"\n"):
            if not line:
                break
            key, _, value = line.partition(b" ")
            if key == b"tree":
                tree = value.decode("ascii")
            elif key == b"parent":
                parents.append(value.decode("ascii"))
        if tree is None or not SHA.fullmatch(tree) or not all(SHA.fullmatch(p) for p in parents):
            raise ToolError(f"commit {sha[:12]} does not parse")
        return tree, tuple(parents)

    def tree(self, sha: str) -> list[tuple[str, bytes, str]]:
        """(mode, name, sha) of each entry."""
        body, out, pos = self.get(sha, "tree"), [], 0
        while pos < len(body):
            space = body.index(b" ", pos)
            nul = body.index(b"\0", space)
            out.append((body[pos:space].decode("ascii"), body[space + 1:nul], body[nul + 1:nul + 21].hex()))
            pos = nul + 21
        return out

    def entry(self, commit: str, path: str) -> tuple[str, str] | None:
        """(mode, sha) at `path` in `commit`, or None when there is nothing there."""
        mode, sha = "40000", self.commit(commit)[0]
        for part in path.split("/"):
            if mode != "40000":
                return None
            found = [(m, s) for m, n, s in self.tree(sha) if n == part.encode("utf-8")]
            if not found:
                return None
            mode, sha = found[0]
        return mode, sha

    def file(self, commit: str, path: str) -> bytes | None:
        found = self.entry(commit, path)
        if found is None or found[0] not in ("100644", "100755"):
            return None
        return self.get(found[1], "blob")

    def files(self, commit: str, top: str) -> dict[str, tuple[str, str]]:
        """Every entry under `top` in `commit`, as path -> (mode, sha); a non-file entry raises."""
        found = self.entry(commit, top)
        if found is None:
            return {}
        if found[0] != "40000":
            raise ToolError(f"{top} is not a directory in {commit[:12]}")
        out: dict[str, tuple[str, str]] = {}
        stack = [(top, found[1])]
        while stack:
            prefix, sha = stack.pop()
            for mode, name, child in self.tree(sha):
                path = f"{prefix}/{name.decode('utf-8')}"
                if mode == "40000":
                    stack.append((path, child))
                elif mode in ("100644", "100755"):
                    out[path] = (mode, child)
                else:
                    raise ToolError(f"{path} in {commit[:12]} is not a plain file")
        return out

    def last_added(self, tip: str, path: str) -> str | None:
        """The latest first-parent commit from `tip` that added `path`: it holds it and its first parent
        does not (or it has none)."""
        sha = tip
        for _ in range(WALK_LIMIT):
            _, parents = self.commit(sha)
            here = self.entry(sha, path) is not None
            before = self.entry(parents[0], path) is not None if parents else False
            if here and not before:
                return sha
            if not parents:
                return None
            sha = parents[0]
        raise ToolError(f"the history from {tip[:12]} is longer than {WALK_LIMIT} first-parent commits")


def gh_runner(binary: str | None = None, environ: Mapping[str, str] = os.environ) \
        -> Callable[[list, str | None], subprocess.CompletedProcess]:
    """A `run` for GhApi that calls the trusted gh with the built environment."""
    problem = gh_config_problem(environ)
    if problem:
        raise ToolError(problem)
    path = binary or trusted("gh")
    env = gh_env(environ)

    def run(argv: list, stdin: str | None) -> subprocess.CompletedProcess:
        if not argv or argv[0] != "gh":
            raise ToolError("the gate runs only gh")
        return subprocess.run([path, *argv[1:]], input=stdin, capture_output=True, text=True,
                              timeout=GH_TIMEOUT_SECONDS, env=env)

    return run


def private_tempdir() -> Path:
    """A fresh 0700 directory under the system temp dir; the caller removes it."""
    return Path(tempfile.mkdtemp(prefix="trimtab-gate-"))


def remove_tree(path: Path) -> None:
    # Cleanup of a private scratch fetch only: no decision reads it after this, and a leftover
    # directory under the system temp dir is the most a failure here can cost.
    shutil.rmtree(path, ignore_errors=True)
