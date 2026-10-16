"""The own-PR list behind guard-mcp's `set_monitor` check (ADR 0028).

A PostToolUse hook on `Bash` records each PR the session itself created; guard-mcp passes
`mcp__ccd_pr__set_monitor` only for a `url` on that list, and denies otherwise. This module
holds the pure part both sides share: what counts as a create, the canonical key a PR URL
reduces to, the list's format and bounds, and the verdict for one call. The file I/O lives
at the edges that call it.

Everything here is data in, value out. Command text, stdout and the call's arguments are
untrusted: they are reduced to a `PrKey` by fixed rules and nothing else is kept. A doubt
anywhere yields no key, which records nothing and denies.

Nothing here prints. A verdict carries a fixed code, never a URL or a session ID.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

LIST_VERSION = 1
TTL_SECONDS = 2 * 24 * 60 * 60
CAP = 30
# An entry stamped further ahead than this is not trusted, so a clock set back cannot keep one alive.
CLOCK_SKEW_SECONDS = 300
MAX_STDOUT_BYTES = 1 << 20

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
PULL_PATH = re.compile(r"/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})/pull/([1-9][0-9]{0,9})/?")
PULLS_ENDPOINT = re.compile(r"/?repos/[^/\s]+/[^/\s]+/pulls/?")

# Rejected anywhere in a create command: each lets text other than gh's own reach stdout,
# or makes the command something other than what it reads as.
UNSAFE_CHARS = ("$", "`", "\\", "\n", "\r")
GLOB_CHARS = set("*?[{")
PORCELAIN_REFUSED = {"--web", "-w", "--dry-run"}
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


class SessionIdError(PrMonitorError):
    """A session ID is missing or not of the expected shape."""


@dataclass(frozen=True, order=True)
class PrKey:
    host: str
    owner: str
    repo: str
    number: int


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


def _porcelain_create(tokens: list[str]) -> bool:
    if tokens[:3] != ["gh", "pr", "create"]:
        return False
    return not any(_flag_name(arg) in PORCELAIN_REFUSED for arg in tokens[3:])


def _api_create(tokens: list[str]) -> bool:
    """`gh api` with a POST to `repos/<o>/<r>/pulls` and nothing that reshapes its stdout."""
    if tokens[:2] != ["gh", "api"]:
        return False
    args, method, path, fields = tokens[2:], None, None, False
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
            return False
        if name in API_VALUED:
            if not eq:
                if i + 1 >= len(args):
                    return False
                i += 1
                value = args[i]
            if name in ("-X", "--method"):
                method = value.upper()
            fields = fields or name in API_FIELDS
        elif arg.startswith("-"):
            return False  # an option this version does not know
        elif path is None:
            path = arg
        else:
            return False
        i += 1
    if path is None or not PULLS_ENDPOINT.fullmatch(path.split("?", 1)[0]):
        return False
    # gh api defaults to GET, and to POST once any field or input is given.
    return (method or ("POST" if fields else "GET")) == "POST"


def _create_form(command: str) -> str | None:
    """'porcelain' | 'api' when the command is an allowed create, else None.

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
    if _porcelain_create(create):
        return "porcelain"
    if _api_create(create):
        return "api"
    return None


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
    JSON object whose `html_url`, `number` and `base.repo.full_name` agree.
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
    form = _create_form(command)
    if form == "porcelain":
        lines = stdout.strip().splitlines()
        return canonical(lines[0]) if len(lines) == 1 else None
    if form == "api":
        return _key_from_api_response(stdout)
    return None


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
    """The entries of a stored list. Raises ListFormatError for anything this version did not write."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise ListFormatError("the list is not JSON") from err
    if not isinstance(data, dict) or set(data) != {"v", "entries"} or data["v"] != LIST_VERSION:
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
