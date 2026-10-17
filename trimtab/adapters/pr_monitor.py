"""The own-PR list behind guard-mcp's `set_monitor` check (ADR 0028).

A PostToolUse hook on `Bash` records each PR the session itself created; guard-mcp passes
`mcp__ccd_pr__set_monitor` only for a `url` on that list, and denies otherwise.

The pure part both sides share comes first: what counts as a create, the canonical key a
PR URL reduces to, the list's format and bounds, and the verdict for one call. It is data
in, value out. Command text, stdout and the call's arguments are untrusted: they are
reduced to a `PrKey` by fixed rules and nothing else is kept. A doubt anywhere yields no
key, which records nothing and denies.

The edge comes last, run by the hooks through hooks/run_module.py (ADR 0035) as the module
`trimtab.adapters.pr_monitor` with `record` or `check`:

    record   PostToolUse (Bash), from hooks/pr-monitor-record.sh. Writes nothing to stdout;
             exits 0, or 1 with one line on stderr when a create was seen and not recorded.
    check    called by guard-mcp for set_monitor. Prints one verdict code on stdout and
             exits 0; guard-mcp passes only on exit 0 and `pass`. Any error exits 1.

The list lives at `<config dir>/guard-state/pr-monitor/<sha256 of session_id>.json`, where
guard-paths keeps a session's own tools out. Hooks run as the same Unix user, so the
directories and files are checked (lstat, owner, mode, O_NOFOLLOW) on every use, and any
doubt denies. No output, at any level, carries a URL, a session ID or its hash.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

LIST_VERSION = 1
TTL_SECONDS = 2 * 24 * 60 * 60
CAP = 30
# An entry stamped further ahead than this is not trusted, so a clock set back cannot keep one alive.
CLOCK_SKEW_SECONDS = 300
MAX_STDOUT_BYTES = 1 << 20

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_REPO = r"[A-Za-z0-9._-]{1,100}"
OWNER, REPO_NAME = re.compile(_OWNER), re.compile(_REPO)
PULL_PATH = re.compile(rf"/({_OWNER})/({_REPO})/pull/([1-9][0-9]{{0,9}})/?")
PULLS_ENDPOINT = re.compile(r"/?repos/([^/\s]+)/([^/\s]+)/pulls/?")
# The host gh talks to when a command names none. A create sent to another host
# must name it (`--hostname`, or `-R <host>/<owner>/<repo>`), and the PR URL it
# prints must be on that host: a server only vouches for its own PRs.
DEFAULT_HOST = "github.com"

# Rejected anywhere in a create command: each lets text other than gh's own reach stdout,
# or makes the command something other than what it reads as.
UNSAFE_CHARS = ("$", "`", "\\", "\n", "\r")
GLOB_CHARS = set("*?[{")
PORCELAIN_REFUSED = {"--web", "-w", "--dry-run"}
REPO_FLAGS = {"-R", "--repo"}
API_VALUED = {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input",
              "-q", "--jq", "-t", "--template", "--hostname", "-p", "--preview", "--cache"}
API_FIELDS = {"-f", "--raw-field", "-F", "--field", "--input"}
# Each reshapes or adds to stdout, so the response is no longer gh's plain JSON object.
API_REFUSED = {"-q", "--jq", "-t", "--template", "-i", "--include", "--paginate", "--silent", "--verbose"}

SET_MONITOR_KEYS = {"auto_fix", "address_comments", "auto_archive_on_close", "url"}
PR_SELECTING_KEYS = {"auto_fix", "address_comments", "url"}


class PrMonitorError(Exception):
    """Base of this module's errors."""


class ListFormatError(PrMonitorError):
    """A stored list is not one this version wrote."""


class UnknownVersionError(ListFormatError):
    """A list with a well-formed version number other than this module's: another version of
    the module wrote it, so this one neither reads it nor prunes it while it is in use."""


class SessionIdError(PrMonitorError):
    """A session ID is missing or not of the expected shape."""


@dataclass(frozen=True, order=True)
class PrKey:
    host: str
    owner: str
    repo: str
    number: int


@dataclass(frozen=True)
class Target:
    """Where a create command sent the PR: the host, and the repository when the command names
    one literally (lower-cased owner and name), else None."""
    host: str
    repo: tuple[str, str] | None


@dataclass(frozen=True)
class Entry:
    key: PrKey
    recorded_at: int


class Verdict(Enum):
    PASS = "pass"
    NOT_RECORDED = "not-recorded"  # a url this session did not create, or one that has expired
    NO_URL = "no-url"  # a PR-selecting call without url
    BAD_ARGUMENTS = "bad-arguments"  # anything this version does not read as a set_monitor call


def canonical(url: object) -> PrKey | None:
    """The key a PR URL reduces to, or None when it is not exactly an https PR page URL."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or "%" in url or any(c.isspace() or not c.isprintable() or ord(c) > 126 for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc or ":" in parts.netloc:
        return None
    host = parts.netloc.lower()
    match = PULL_PATH.fullmatch(parts.path)
    if not HOST.fullmatch(host) or match is None:
        return None
    owner, repo, number = match.groups()
    if repo in (".", ".."):
        return None
    return PrKey(host, owner.lower(), repo.lower(), int(number))


def _segments(command: str) -> list[list[str]] | None:
    """The command split at `&&`, or None for any other operator, an empty segment or a parse error."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments, current = [], []
    try:
        for token in lexer:
            if token and set(token) <= set("();<>|&"):
                if token != "&&" or not current:
                    return None
                segments.append(current)
                current = []
            else:
                current.append(token)
    except ValueError:
        return None
    if not current:
        return None
    segments.append(current)
    return segments


def _is_cd(tokens: list[str]) -> bool:
    if len(tokens) != 2 or tokens[0] != "cd":
        return False
    path = tokens[1]
    return bool(path) and not path.startswith("-") and not GLOB_CHARS & set(path)


def _is_git_push(tokens: list[str]) -> bool:
    return tokens[:2] == ["git", "push"] and "--porcelain" not in tokens[2:]


def _flag_name(arg: str) -> str:
    return arg.partition("=")[0] if arg.startswith("--") else arg


def _repo_target(value: str) -> Target | None:
    """The target `-R/--repo` names: `OWNER/REPO`, `HOST/OWNER/REPO`, or an https URL."""
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return None
        if parts.scheme != "https" or "@" in parts.netloc or ":" in parts.netloc:
            return None
        host, segments = parts.netloc.lower(), [s for s in parts.path.split("/") if s]
    else:
        segments = value.split("/")
        host = DEFAULT_HOST if len(segments) == 2 else segments[0].lower()
        segments = segments[-2:] if len(segments) in (2, 3) else []
    if len(segments) != 2 or not HOST.fullmatch(host):
        return None
    owner, repo = segments
    if not OWNER.fullmatch(owner) or not REPO_NAME.fullmatch(repo) or repo in (".", ".."):
        return None
    return Target(host, (owner.lower(), repo.lower()))


def _porcelain_create(tokens: list[str]) -> Target | None:
    """The target of a `gh pr create`, or None when it is not a plain create or names no clear one."""
    if tokens[:3] != ["gh", "pr", "create"]:
        return None
    args, repos = tokens[3:], []
    for i, arg in enumerate(args):
        name = _flag_name(arg)
        if name in PORCELAIN_REFUSED:
            return None
        if name in REPO_FLAGS:
            if arg.startswith("--repo="):
                repos.append(arg.partition("=")[2])
            elif i + 1 < len(args):
                repos.append(args[i + 1])
            else:
                return None
        elif arg.startswith("-R") and len(arg) > 2:
            repos.append(arg[2:].removeprefix("="))
    if len(repos) > 1:
        return None  # which one gh used is not this parser's guess to make
    return _repo_target(repos[0]) if repos else Target(DEFAULT_HOST, None)


def _api_create(tokens: list[str]) -> Target | None:
    """The target of a `gh api` POST to `repos/<o>/<r>/pulls` with nothing that reshapes its
    stdout, or None."""
    if tokens[:2] != ["gh", "api"]:
        return None
    args, method, path, fields, host = tokens[2:], None, None, False, DEFAULT_HOST
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
        elif len(arg) > 2 and arg[0] == "-" and arg[:2] in API_VALUED | API_REFUSED:
            name, eq, value = arg[:2], "=", arg[2:].removeprefix("=")
        else:
            name, eq, value = arg, "", ""
        if name in API_REFUSED:
            return None
        if name in API_VALUED:
            if not eq:
                if i + 1 >= len(args):
                    return None
                i += 1
                value = args[i]
            if name in ("-X", "--method"):
                method = value.upper()
            if name == "--hostname":
                host = value.lower()
            fields = fields or name in API_FIELDS
        elif arg.startswith("-"):
            return None  # an option this version does not know
        elif path is None:
            path = arg
        else:
            return None
        i += 1
    match = PULLS_ENDPOINT.fullmatch(path.split("?", 1)[0]) if path is not None else None
    # gh api defaults to GET, and to POST once any field or input is given.
    if match is None or (method or ("POST" if fields else "GET")) != "POST" or not HOST.fullmatch(host):
        return None
    owner, repo = match.groups()
    if (owner, repo) == ("{owner}", "{repo}"):
        return Target(host, None)  # gh fills these from the checkout's remote
    if not OWNER.fullmatch(owner) or not REPO_NAME.fullmatch(repo):
        return None
    return Target(host, (owner.lower(), repo.lower()))


def _create_form(command: str) -> tuple[str, Target] | None:
    """('porcelain' | 'api', target) when the command is an allowed create, else None.

    Allowed: `[cd <literal path> &&] [git push [args] &&] <create>`, nothing after the create.
    """
    if any(c in command for c in UNSAFE_CHARS):
        return None
    segments = _segments(command)
    if not segments:
        return None
    if any("=" in tokens[0] for tokens in segments):
        return None  # a VAR=value prefix
    *prefixes, create = segments
    if prefixes and _is_cd(prefixes[0]):
        prefixes = prefixes[1:]
    if prefixes and _is_git_push(prefixes[0]):
        prefixes = prefixes[1:]
    if prefixes:
        return None
    target = _porcelain_create(create)
    if target is not None:
        return "porcelain", target
    target = _api_create(create)
    if target is not None:
        return "api", target
    return None


def _on_target(key: PrKey | None, target: Target) -> PrKey | None:
    """The key, when it is a PR on the host the create went to and, if the command named a
    repository, in that repository; else None."""
    if key is None or key.host != target.host:
        return None
    if target.repo is not None and (key.owner, key.repo) != target.repo:
        return None
    return key


def _key_from_api_response(stdout: str) -> PrKey | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    key, number = canonical(data.get("html_url")), data.get("number")
    if key is None or type(number) is not int or number != key.number:
        return None
    base = data.get("base")
    if base is not None:
        repo = base.get("repo") if isinstance(base, dict) else None
        name = repo.get("full_name") if isinstance(repo, dict) else None
        if name is not None and (not isinstance(name, str) or name.lower() != f"{key.owner}/{key.repo}"):
            return None
    return key


def created_pr(payload: object) -> PrKey | None:
    """The PR a successful Bash call created, from a PostToolUse payload, or None.

    The key comes only from the call's stdout, never from its command text: for `gh pr
    create`, stdout is exactly one line and that line is a PR URL; for `gh api`, it is one
    JSON object whose `html_url`, `number` and `base.repo.full_name` agree. The command
    text only bounds it: the PR must be on the host the create went to, and in the
    repository the command names, if it names one.
    """
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool_input, response = payload.get("tool_input"), payload.get("tool_response")
    if not isinstance(tool_input, dict) or not isinstance(response, dict):
        return None
    command, stdout = tool_input.get("command"), response.get("stdout")
    if not isinstance(command, str) or not isinstance(stdout, str):
        return None
    if response.get("interrupted") not in (None, False):
        return None
    if response.get("backgroundTaskId") or response.get("persistedOutputPath"):
        return None  # the output seen is not the whole of it
    if len(stdout.encode("utf-8", errors="replace")) > MAX_STDOUT_BYTES:
        return None
    created = _create_form(command)
    if created is None:
        return None
    form, target = created
    if form == "porcelain":
        lines = stdout.strip().splitlines()
        return _on_target(canonical(lines[0]), target) if len(lines) == 1 else None
    return _on_target(_key_from_api_response(stdout), target)


def list_name(session_id: object) -> str:
    """The list's file name for a session: the SHA-256 of its ID, so the ID is never written."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise SessionIdError("the session ID is missing or malformed")
    return hashlib.sha256(session_id.encode("ascii")).hexdigest() + ".json"


def is_live(entry: Entry, now: int) -> bool:
    return entry.recorded_at <= now + CLOCK_SKEW_SECONDS and now - entry.recorded_at < TTL_SECONDS


def live(entries: tuple[Entry, ...], now: int) -> tuple[Entry, ...]:
    return tuple(e for e in entries if is_live(e, now))


def is_expired_list(entries: tuple[Entry, ...], now: int) -> bool:
    """True when no entry is live: the whole list may be deleted."""
    return not live(entries, now)


def add(entries: tuple[Entry, ...], key: PrKey, now: int) -> tuple[Entry, ...]:
    """The list with `key` recorded at `now`: expired entries dropped, one entry per PR, newest CAP kept."""
    kept = tuple(e for e in live(entries, now) if e.key != key)
    return (kept + (Entry(key, now),))[-CAP:]


def encode(entries: tuple[Entry, ...]) -> str:
    rows = [{"host": e.key.host, "owner": e.key.owner, "repo": e.key.repo, "number": e.key.number,
             "at": e.recorded_at} for e in entries]
    return json.dumps({"v": LIST_VERSION, "entries": rows}, sort_keys=True, separators=(",", ":")) + "\n"


def decode(text: str) -> tuple[Entry, ...]:
    """The entries of a stored list. Raises ListFormatError for anything this version did not
    write, and its subclass UnknownVersionError for a list carrying another integer version."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise ListFormatError("the list is not JSON") from err
    version = data.get("v") if isinstance(data, dict) else None
    if type(version) is int and version != LIST_VERSION:
        raise UnknownVersionError("the list was written by another version of this module")
    if (not isinstance(data, dict) or set(data) != {"v", "entries"}
            or type(version) is not int or version != LIST_VERSION):  # true and 1.0 equal 1
        raise ListFormatError("the list is not a version-1 list")
    rows = data["entries"]
    if not isinstance(rows, list) or len(rows) > CAP:
        raise ListFormatError("the list's entries are malformed")
    entries = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"host", "owner", "repo", "number", "at"}:
            raise ListFormatError("an entry is malformed")
        number, at = row["number"], row["at"]
        if type(number) is not int or type(at) is not int or at < 0:
            raise ListFormatError("an entry is malformed")
        if not all(isinstance(row[k], str) for k in ("host", "owner", "repo")):
            raise ListFormatError("an entry is malformed")
        key = canonical(f"https://{row['host']}/{row['owner']}/{row['repo']}/pull/{number}")
        if key is None or key != PrKey(row["host"], row["owner"], row["repo"], number):
            raise ListFormatError("an entry is not in canonical form")
        entries.append(Entry(key, at))
    return tuple(entries)


def decide(tool_input: object, entries: tuple[Entry, ...], now: int) -> Verdict:
    """The verdict for one `set_monitor` call, given the session's stored entries."""
    if not isinstance(tool_input, dict) or not set(tool_input) <= SET_MONITOR_KEYS:
        return Verdict.BAD_ARGUMENTS
    for flag in ("auto_fix", "address_comments", "auto_archive_on_close"):
        if flag in tool_input and not isinstance(tool_input[flag], bool):
            return Verdict.BAD_ARGUMENTS
    if not PR_SELECTING_KEYS & set(tool_input):
        # Only the session-wide auto-archive switch, which selects no PR.
        return Verdict.PASS if "auto_archive_on_close" in tool_input else Verdict.BAD_ARGUMENTS
    if "url" not in tool_input:
        return Verdict.NO_URL
    if not isinstance(tool_input["url"], str):
        return Verdict.BAD_ARGUMENTS
    key = canonical(tool_input["url"])
    if key is not None and any(e.key == key for e in live(entries, now)):
        return Verdict.PASS
    return Verdict.NOT_RECORDED


# --- the edge: the list on disk ------------------------------------------------------------

SET_MONITOR = "mcp__ccd_pr__set_monitor"
STATE_PARTS = ("guard-state", "pr-monitor")
LOCK_NAME = ".lock"
TEMP_PREFIX = ".tmp-"
MAX_LIST_BYTES = 64 * 1024
MAX_PRUNE_FILES = 1000
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05
# O_NONBLOCK: a FIFO planted where a list should be must not hang the hook.
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class StateDirError(PrMonitorError):
    """The list's directory is missing, or not one this user alone controls."""


class LockTimeoutError(PrMonitorError):
    """Another writer held the list's lock past the bound."""


def config_dir() -> Path:
    """The Claude config directory: CLAUDE_CONFIG_DIR, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"))


def _private(st: os.stat_result) -> bool:
    return st.st_uid == os.geteuid() and not st.st_mode & 0o077


def state_dir(config: Path, create: bool) -> Path:
    """`<config>/guard-state/pr-monitor`, each level a real directory private to this user.

    With `create`, missing levels are made 0700. Raises StateDirError otherwise, or OSError
    when a level cannot be made.
    """
    path = config
    for part in STATE_PARTS:
        path = path / part
        if create:
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass  # checked below like any existing level
        try:
            st = os.lstat(path)
        except FileNotFoundError as err:
            raise StateDirError("the list directory is missing") from err
        if not stat.S_ISDIR(st.st_mode) or not _private(st):
            raise StateDirError("the list directory is not private to this user")
    return path


def read_list(path: Path) -> tuple[Entry, ...]:
    """The stored entries, or () when there is no list.

    Raises ListFormatError for a list this version did not write or a file that is not a
    private regular file, and OSError for a symlink (O_NOFOLLOW) or an unreadable file.
    """
    try:
        fd = os.open(path, READ_FLAGS)
    except FileNotFoundError:
        return ()
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or not _private(st):
            raise ListFormatError("the list file is not a private regular file")
        data = fh.read(MAX_LIST_BYTES + 1)
    if len(data) > MAX_LIST_BYTES:
        raise ListFormatError("the list is larger than any this version writes")
    try:
        return decode(data.decode("utf-8"))
    except UnicodeDecodeError as err:
        raise ListFormatError("the list is not UTF-8") from err


def _write_list(directory: Path, name: str, entries: tuple[Entry, ...]) -> None:
    """Replace the list atomically: a private temporary file, then a rename over the name."""
    fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=directory)  # 0600, O_EXCL
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encode(entries))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, directory / name)  # replaces a planted symlink itself, never its target
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass  # the rename happened or the file never existed; the original error is re-raised
        raise


class _Lock:
    """An exclusive flock on the directory's lock file, waited for at most LOCK_TIMEOUT_SECONDS."""

    def __init__(self, directory: Path):
        self.path = directory / LOCK_NAME
        self.fd: int | None = None

    def __enter__(self) -> _Lock:
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise LockTimeoutError("the list is locked by another writer") from None
                time.sleep(LOCK_POLL_SECONDS)

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)  # closing releases the flock
            self.fd = None


def _prune(directory: Path, own: str, now: int) -> int:
    """Delete other sessions' expired or unreadable lists, planted symlinks, and stale temporary
    files. A list another version of this module wrote is kept while its file was written
    within the TTL, since that version rewrites it on every record and prunes its own; only
    an untouched one is deleted, so the directory stays bounded after a rollback. Returns how
    many could not be handled; each is left in place."""
    failed = 0
    with os.scandir(directory) as it:
        for i, item in enumerate(it):
            if i >= MAX_PRUNE_FILES:
                break
            if item.name in (own, LOCK_NAME):
                continue
            try:
                if item.is_symlink():
                    stale = True
                elif not item.is_file(follow_symlinks=False):
                    continue
                elif item.name.startswith(TEMP_PREFIX):
                    stale = now - int(item.stat(follow_symlinks=False).st_mtime) >= TTL_SECONDS
                elif item.name.endswith(".json"):
                    try:
                        stale = is_expired_list(read_list(Path(item.path)), now)
                    except UnknownVersionError:
                        stale = now - int(item.stat(follow_symlinks=False).st_mtime) >= TTL_SECONDS
                    except ListFormatError:
                        stale = True
                else:
                    continue
                if stale:
                    os.unlink(item.path)
            except FileNotFoundError:
                continue  # gone already
            except OSError:
                failed += 1
    return failed


def record(payload: object, config: Path, now: int) -> tuple[int, str]:
    """Record the PR a Bash call created, if it created one. Returns (exit code, stderr line)."""
    key = created_pr(payload)
    if key is None:
        return 0, ""
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    try:
        name = list_name(session_id)
        directory = state_dir(config, create=True)
        with _Lock(directory):
            try:
                entries = read_list(directory / name)
            except (ListFormatError, OSError):
                # An unreadable or planted list grants nothing; it is replaced by one that
                # holds only what this session has now been seen to create.
                entries = ()
            _write_list(directory, name, add(entries, key, now))
            unpruned = _prune(directory, name, now)
    except SessionIdError:
        return 1, "pr-monitor: a PR was created, but the session ID is missing or malformed; not recorded"
    except StateDirError:
        return 1, "pr-monitor: a PR was created, but the list directory is not private to this user; not recorded"
    except (LockTimeoutError, OSError) as err:
        return 1, f"pr-monitor: a PR was created, but the list could not be written ({type(err).__name__}); not recorded"
    if unpruned:
        return 1, f"pr-monitor: recorded; {unpruned} expired list(s) could not be removed"
    return 0, ""


def check(payload: object, config: Path, now: int) -> Verdict:
    """The verdict for a PreToolUse payload of set_monitor, reading the session's list."""
    if not isinstance(payload, dict) or payload.get("tool_name") != SET_MONITOR:
        return Verdict.BAD_ARGUMENTS
    tool_input = payload.get("tool_input")
    first = decide(tool_input, (), now)
    if first is not Verdict.NOT_RECORDED:
        return first  # settled without the list: the auto-archive pass, or a call that cannot pass
    try:
        name = list_name(payload.get("session_id"))
        entries = read_list(state_dir(config, create=False) / name)
    except (PrMonitorError, OSError):
        return Verdict.NOT_RECORDED  # no list this session could have written: deny
    return decide(tool_input, entries, now)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in (["record"], ["check"]):
        print("usage: trimtab.adapters.pr_monitor record|check (run through hooks/run_module.py)", file=sys.stderr)
        return 1
    now = int(time.time())
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        payload = None  # judged below like any payload that is not a create or a set_monitor call
    if args == ["check"]:
        try:
            verdict = check(payload, config_dir(), now)
        except Exception as err:  # fail closed: guard-mcp denies on anything but exit 0 and `pass`
            print(f"pr-monitor: internal error ({type(err).__name__})", file=sys.stderr)
            return 1
        print(verdict.value)
        return 0
    try:
        code, message = record(payload, config_dir(), now)
    except Exception as err:  # never 2: a PostToolUse hook has nothing to block
        print(f"pr-monitor: internal error ({type(err).__name__}); nothing recorded", file=sys.stderr)
        return 1
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
