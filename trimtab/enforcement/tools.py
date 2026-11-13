"""The git and gh the gate runs, hardened against what the gated session can write (ADR 0063 §2).

The gate runs where a session can edit checkouts, git config and object files, gh's config, set
variables and put programs on PATH. So:

- **Binaries** are `git` and `gh` from the system directories only: where each resolves, a
  regular file owned by root that only root can write, in a directory only root can write. A
  program a session put on PATH is never run.
- **Environment** is built, never inherited: PATH is the system directories, nothing of git's
  steering or config variables survives, no global or system git config is read. Proxy and CA
  settings are kept, so a host whose egress needs them still reaches GitHub; they can only make
  a read fail, since TLS is verified and every object re-hashed.
- **gh never reads its own config.** `GhAuth` takes the token once (from `GH_TOKEN` or
  `GITHUB_TOKEN`, else `gh auth token -h github.com`), then every gh call runs with a fresh, empty
  `GH_CONFIG_DIR`, `GH_HOST=github.com` and that token. A session-written config (a unix socket,
  another host) takes no part; a forged token only fails the reads.
- **Objects are verified, not trusted.** Every commit, tree and blob a decision rests on is read
  through one long-lived `git cat-file --batch` per repository and its SHA-1 recomputed here, so
  a substituted object file, a replace ref or a graft cannot change what a SHA reads as. Commits
  are parsed as git parses them (the first line names the tree, then only consecutive parents),
  and history is walked here, from verified commits, never by `git log`. A repository with
  alternates or grafts is refused; a linked worktree is read through its common dir, which
  `--git-path` already resolves.
- **The fetch** goes to an explicit `https://github.com/<owner>/<name>.git`, into a fresh bare
  repository under a private temporary directory, with no config but what is pinned here, only the
  https protocol, and gh (with `GhAuth`'s empty config) as the one credential helper.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import select
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from trimtab.planscope import GIT_PINNED

SYSTEM_DIRS = ("/usr/bin", "/bin")
GIT_KEEP = ("HOME", "LANG", "LC_ALL")
# Egress settings, kept so a proxied host still reaches GitHub. A proxy alone can only make a read fail,
# since TLS is verified against the trusted CAs; so a CA setting is kept only when what it names is root-only.
PROXY_KEEP = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
              "ALL_PROXY", "all_proxy")
CA_KEEP = ("SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE")
# What `gh auth token` needs to reach a keyring-held token (the Secret Service on a desktop).
KEYRING_KEEP = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
OBJECT_LIMIT = 64 << 20  # bytes in one object the gate reads; past it, refused before it is buffered
READ_DEADLINE_SECONDS = 10  # for one object from the batch process
GIT_TIMEOUT_SECONDS = 10
GH_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 60
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
TOKEN = re.compile(r"[A-Za-z0-9_]{20,255}")
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
    """The absolute path of `name` in a system directory, where what it resolves to is root-owned and writable
    only by root, in a root-only directory, and the directory holding the name is root-only too."""
    for directory in dirs:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        real = os.path.realpath(path)
        if os.access(path, os.X_OK) and _root_only(directory) and _root_only(real) \
                and _root_only(os.path.dirname(real)):
            return path
        raise ToolError(f"{path} is not a root-owned binary in a root-only directory")
    raise ToolError(f"no {name} in {', '.join(dirs)}")


def _root_only_tree(path: str) -> bool:
    """`path` and every directory above it are root-owned and writable only by root."""
    real = os.path.realpath(path)
    while True:
        if not _root_only(real):
            return False
        parent = os.path.dirname(real)
        if parent == real:
            return True
        real = parent


def _network(environ: Mapping[str, str]) -> dict[str, str]:
    env = {k: environ[k] for k in PROXY_KEEP if k in environ}
    for key in CA_KEEP:
        # Resolved once, and that one path is both judged and kept: a session-owned link to a root-owned CA
        # could be re-pointed after the check, or between two resolutions, but the resolved path cannot be.
        if key in environ:
            real = os.path.realpath(environ[key])
            if _root_only_tree(real):
                env[key] = real
    return env


def git_env(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Only what git needs and nothing that steers it: built from an allowlist, never by removal."""
    env = {k: environ[k] for k in GIT_KEEP if k in environ}
    env.update(_network(environ))
    # No system or global config (~/.gitconfig is the session's to write), and no lazy fetch: a repository the
    # session configured as a partial clone would otherwise run its fetch command for any missing object.
    env.update(PATH=":".join(SYSTEM_DIRS), GIT_NO_REPLACE_OBJECTS="1", GIT_LITERAL_PATHSPECS="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_NO_LAZY_FETCH="1")
    return env


def _stderr(done: subprocess.CompletedProcess) -> str:
    text = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
    text = " ".join(text.split())
    return f": {text[:STDERR_CHARS]}" if text else ""


class GhAuth:
    """gh's token, taken once, and an empty config dir every later gh call uses instead of the user's."""

    def __init__(self, environ: Mapping[str, str] = os.environ, binary: str | None = None,
                 token: str | None = None):
        self.binary = binary or trusted("gh")
        self.token = token or environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or self._read(environ)
        if not TOKEN.fullmatch(self.token):
            raise ToolError("gh's token for github.com has an unexpected shape")
        self.config_dir = Path(tempfile.mkdtemp(prefix="trimtab-gh-"))
        atexit.register(remove_tree, self.config_dir)
        self.env = {**_network(environ), "HOME": str(self.config_dir), "PATH": ":".join(SYSTEM_DIRS),
                    "GH_CONFIG_DIR": str(self.config_dir), "GH_HOST": "github.com", "GH_TOKEN": self.token,
                    "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}

    def _read(self, environ: Mapping[str, str]) -> str:
        # The user's own config, once, for the token alone: a forged one only makes every read fail.
        env = {k: environ[k] for k in ("HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", *KEYRING_KEEP) if k in environ}
        env.update(_network(environ), PATH=":".join(SYSTEM_DIRS), GH_PROMPT_DISABLED="1")
        try:
            done = subprocess.run([self.binary, "auth", "token", "-h", "github.com"], capture_output=True,
                                  text=True, timeout=GH_TIMEOUT_SECONDS, env=env)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ToolError(f"gh auth token failed ({type(err).__name__})") from err
        if done.returncode != 0:
            raise ToolError("gh has no token for github.com")
        return done.stdout.strip()


class Git:
    """Hardened git, checking each repository once for alternates, grafts and shallowness."""

    def __init__(self, binary: str | None = None, environ: Mapping[str, str] = os.environ,
                 timeout: float = GIT_TIMEOUT_SECONDS):
        self._binary = binary or trusted("git")
        self._env = git_env(environ)
        self._timeout = timeout
        self._checked: set[tuple[str, bool]] = set()

    def _pins(self) -> list[str]:
        # No hooks, and as little transport as a command line can pin. GIT_NO_LAZY_FETCH and check()'s
        # partial-clone refusal are what stop a lazy fetch; these pins are a further layer, not a stop on
        # their own, since a repository's `protocol.<name>.allow` outranks `protocol.allow`.
        extra = (("core.hooksPath", os.devnull), ("protocol.allow", "never"), ("core.sshCommand", ""))
        return [a for key, value in (*GIT_PINNED, *extra) for a in ("-c", f"{key}={value}")]

    def argv(self, repo: Path, *args: str) -> list[str]:
        return [self._binary, "-C", str(repo), *self._pins(), *args]

    @property
    def env(self) -> dict[str, str]:
        return dict(self._env)

    def _raw(self, repo: Path, *args: str, stdin: bytes | None = None, env: dict | None = None,
             timeout: float | None = None) -> bytes:
        try:
            done = subprocess.run(self.argv(repo, *args), capture_output=True, timeout=timeout or self._timeout,
                                  env=env or self._env, input=stdin)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ToolError(f"git {args[0]} in {repo.name} failed ({type(err).__name__})") from err
        if done.returncode != 0:
            raise ToolError(f"git {args[0]} in {repo.name} exited {done.returncode}{_stderr(done)}")
        return done.stdout

    def _path(self, repo: Path, name: str) -> Path:
        return Path(self._raw(repo, "rev-parse", "--path-format=absolute", "--git-path", name)
                    .decode("utf-8", "replace").strip())

    def check(self, repo: Path, allow_shallow: bool = False) -> None:
        key = (str(repo.resolve()), allow_shallow)
        if key in self._checked:
            return
        if not allow_shallow and self._raw(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise ToolError(f"{repo.name} is a shallow clone, whose history can be cut")
        # --git-path resolves these into a linked worktree's common dir, where they would live.
        for name, why in (("info/grafts", "a grafts file, which rewrites history"),
                          ("objects/info/alternates", "alternates, whose objects live elsewhere")):
            if os.path.lexists(self._path(repo, name)):
                raise ToolError(f"{repo.name} has {why}")
        # A partial clone fetches what it lacks, running whatever command its config names.
        for entry in self._raw(repo, "config", "--list", "-z").split(b"\0"):
            name = entry.split(b"\n", 1)[0].decode("utf-8", "replace").lower()
            if name == "extensions.partialclone" or (name.startswith("remote.") and name.endswith(".promisor")):
                raise ToolError(f"{repo.name} is configured as a partial clone, which fetches what it lacks")
        self._checked.add(key)

    def is_repository(self, repo: Path) -> bool:
        """Whether `repo` is inside a git repository at all, asked without the safety checks `check` adds."""
        try:
            self._raw(repo, "rev-parse", "--git-dir")
        except ToolError:
            return False
        return True

    def ls_remote(self, cwd: Path, url: str, ref: str, timeout: float, auth: "GhAuth | None" = None) -> str:
        """The SHA a remote ref points at, under `timeout`, read without the object checks: nothing it
        returns is read as an object. It runs in a bare repository made here inside `cwd`, so only
        this environment's pinned configuration takes part and no `insteadOf` of a repository above can
        answer (ADR 0063 §3, review round 2). `cwd` must be a private directory of the caller's own.

        With `auth`, the clean fetch's pinned gh credential helper applies, so a private repository
        answers; without it the read is anonymous and a private repository fails closed."""
        env = dict(self._env)
        pins = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.sshCommand=",
                "-c", "http.sslVerify=true"]
        if auth is not None:
            env.update({k: auth.env[k] for k in ("GH_CONFIG_DIR", "GH_HOST", "GH_TOKEN")})
            pins += ["-c", "credential.helper=",
                     "-c", f"credential.helper=!{auth.binary} auth git-credential"]
        # Git finds a repository by searching upward from its directory and then reads that repository's
        # configuration, so a read made in a plain temp directory would take an `insteadOf` planted in any
        # repository above it. A bare repository made here is found first and holds no configuration but
        # what the pins give (the same shape `fetch_clean` uses).
        probe = Path(cwd) / "ls-remote.git"
        try:
            os.mkdir(probe, 0o700)  # exclusively: `git init` on an existing directory keeps a planted config
        except OSError as err:
            raise ToolError(f"the ls-remote probe directory could not be made ({type(err).__name__})") from err
        self._raw(Path(cwd), "init", "-q", "--bare", str(probe), env=env)
        out = self._raw(probe, *pins, "ls-remote", "--", url, ref, env=env,
                        timeout=timeout).decode("utf-8", "replace")
        for line in out.split("\n"):
            sha, _, name = line.partition("\t")
            if name.strip() == ref and len(sha) == 40:
                return sha
        raise ToolError(f"ls-remote named no {ref} for the repository")

    def config_get(self, repo: Path, key: str) -> str:
        """One config value, read without the object checks: it names nothing the gate then trusts."""
        return self._raw(repo, "config", "--get", key).decode("utf-8", "replace").strip()

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

    def fetch_clean(self, repository: str, branch: str, into: Path, auth: GhAuth) -> str:
        """Fetch `branch` of github.com/`repository` into a new bare repository at `into`, depth 1, with
        nothing but pinned config. Returns the fetched tip. The caller compares it with the server's."""
        if not REPOSITORY.fullmatch(repository) or ".." in repository:
            raise ToolError("not owner/name to fetch")
        if not BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".", "/", ".lock")):
            raise ToolError("not a branch name to fetch")
        env = {**self._env, **{k: auth.env[k] for k in ("GH_CONFIG_DIR", "GH_HOST", "GH_TOKEN")}}
        self._raw(into.parent, "init", "-q", "--bare", str(into), env=env)
        fetch_pins = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.sshCommand=",
                      "-c", "credential.helper=", "-c", f"credential.helper=!{auth.binary} auth git-credential",
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
    """Objects read by SHA through one `cat-file --batch` and re-hashed here; history walked from verified commits."""

    def __init__(self, git: Git, repo: Path, deadline: float = READ_DEADLINE_SECONDS):
        self._repo = repo
        self._deadline = deadline
        self._cache: dict[str, tuple[str, bytes]] = {}
        self._buffer = bytearray()
        self._broken: str | None = None  # once a read fails, the stream's framing is lost: every later read refuses
        try:
            self._proc = subprocess.Popen(git.argv(repo, "cat-file", "--batch"), stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git.env, bufsize=0)
        except OSError as err:
            raise ToolError(f"git cat-file in {repo.name} failed ({type(err).__name__})") from err

    def close(self) -> None:
        """End the batch process and release its pipes; each step on its own, so one failure skips none."""
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                pipe.close()
            except OSError:
                continue  # a pipe the process already broke: nothing left to release
        try:
            self._proc.wait(timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def __del__(self):
        try:
            self.close()
        except (OSError, ValueError, AttributeError):
            pass  # interpreter shutdown: the process goes with us

    def _fill(self, until: float, want: int = 65536) -> None:
        """Read more from the batch process into the buffer, or ToolError once `until` passes or it ends."""
        fd = self._proc.stdout.fileno()
        left = until - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise ToolError(f"git cat-file in {self._repo.name} did not answer within {self._deadline} s")
        chunk = os.read(fd, min(max(65536, want), 1 << 20))
        if not chunk:
            raise ToolError(f"git cat-file in {self._repo.name} ended early")
        self._buffer += chunk  # a bytearray grows in place: linear in the object's size

    def _take(self, n: int, until: float) -> bytes:
        """Exactly `n` bytes from the batch process."""
        while len(self._buffer) < n:
            self._fill(until, n - len(self._buffer))
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    def _line(self, until: float) -> bytes:
        while b"\n" not in self._buffer:
            if len(self._buffer) > 4096:
                raise ToolError(f"git cat-file in {self._repo.name} sent a header past 4096 bytes")
            self._fill(until)
        end = self._buffer.index(b"\n")
        line = bytes(self._buffer[:end])
        del self._buffer[:end + 1]
        return line

    def _read_one(self, sha: str) -> tuple[str, bytes]:
        if self._broken:
            raise ToolError(f"{self._repo.name}'s object reads stopped after an earlier failure: {self._broken}")
        try:
            return self._read_framed(sha)
        except ToolError as err:
            self._broken = str(err)
            raise

    def _read_framed(self, sha: str) -> tuple[str, bytes]:
        until = time.monotonic() + self._deadline
        try:
            self._proc.stdin.write(sha.encode("ascii") + b"\n")
            self._proc.stdin.flush()
            header = self._line(until).decode("ascii").split(" ")
            if len(header) != 3 or header[0] != sha:
                raise ToolError(f"{self._repo.name} has no object {sha[:12]}")
            kind, size = header[1], int(header[2])
            if not 0 <= size <= OBJECT_LIMIT:
                raise ToolError(f"{self._repo.name}'s object {sha[:12]} is past {OBJECT_LIMIT} bytes")
            body = self._take(size, until)
            trailer = self._take(1, until)
        except (OSError, ValueError, UnicodeDecodeError) as err:
            raise ToolError(f"{self._repo.name}'s object {sha[:12]} could not be read ({type(err).__name__})") from err
        if trailer != b"\n":
            raise ToolError(f"{self._repo.name}'s object {sha[:12]} was cut short")
        if hashlib.sha1(f"{kind} {size}\0".encode("ascii") + body).hexdigest() != sha:
            raise ToolError(f"{self._repo.name}'s object {sha[:12]} does not hash to its name")
        return kind, body

    def get(self, sha: str, kind: str) -> bytes:
        if not SHA.fullmatch(sha):
            raise ToolError("an object name is not a full SHA-1")
        if sha not in self._cache:
            self._cache[sha] = self._read_one(sha)
        found, body = self._cache[sha]
        if found != kind:
            raise ToolError(f"{sha[:12]} is a {found}, not a {kind}")
        return body

    def commit(self, sha: str) -> tuple[str, tuple[str, ...]]:
        """(tree, parents) of a commit, as git reads them: the first header line names the tree, then only
        consecutive `parent` lines; anything else ends the parents."""
        lines = self.get(sha, "commit").split(b"\n")
        first = lines[0].split(b" ", 1) if lines else []
        if len(first) != 2 or first[0] != b"tree":
            raise ToolError(f"commit {sha[:12]} does not open with its tree")
        tree = first[1].decode("ascii", "replace")
        parents = []
        for line in lines[1:]:
            key, _, value = line.partition(b" ")
            if key != b"parent":
                break
            parents.append(value.decode("ascii", "replace"))
        if not SHA.fullmatch(tree) or not all(SHA.fullmatch(p) for p in parents):
            raise ToolError(f"commit {sha[:12]} does not parse")
        return tree, tuple(parents)

    def tree(self, sha: str) -> list[tuple[str, bytes, str]]:
        """(mode, name, sha) of each entry."""
        body, out, pos = self.get(sha, "tree"), [], 0
        try:
            while pos < len(body):
                space = body.index(b" ", pos)
                nul = body.index(b"\0", space)
                if nul + 21 > len(body):
                    raise ValueError("truncated entry")
                out.append((body[pos:space].decode("ascii"), body[space + 1:nul], body[nul + 1:nul + 21].hex()))
                pos = nul + 21
        except (ValueError, UnicodeDecodeError) as err:
            raise ToolError(f"tree {sha[:12]} does not parse") from err
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
                try:
                    path = f"{prefix}/{name.decode('utf-8')}"
                except UnicodeDecodeError as err:
                    raise ToolError(f"a path under {top} in {commit[:12]} is not UTF-8") from err
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


def gh_runner(auth: GhAuth | None = None) -> Callable[[list, str | None], subprocess.CompletedProcess]:
    """A `run` for GhApi that calls the trusted gh with `auth`'s empty config, host and token."""
    auth = auth or GhAuth()

    def run(argv: list, stdin: str | None) -> subprocess.CompletedProcess:
        if not argv or argv[0] != "gh":
            raise ToolError("the gate runs only gh")
        return subprocess.run([auth.binary, *argv[1:]], input=stdin, capture_output=True, text=True,
                              timeout=GH_TIMEOUT_SECONDS, env=auth.env)

    return run


def private_tempdir() -> Path:
    """A fresh 0700 directory under the system temp dir; the caller removes it."""
    return Path(tempfile.mkdtemp(prefix="trimtab-gate-"))


def remove_tree(path: Path) -> None:
    # Cleanup of private scratch only: no decision reads it after this, and a leftover directory
    # under the system temp dir is the most a failure here can cost.
    shutil.rmtree(path, ignore_errors=True)
