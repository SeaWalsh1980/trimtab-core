"""PreToolUse adapter: check the PR body a `gh pr create|edit` command would send.

It reads the REST form too: `gh api` POST to `repos/<o>/<r>/pulls` (create)
and PATCH to `repos/<o>/<r>/pulls/<n>` (edit), however the owner, repository
and number are spelled (a shell variable, gh's `{owner}/{repo}`, a full URL).
The body is the `--input <file>` JSON object's `body` when `--input` is
given (gh then sends fields as query parameters), and otherwise `-f body=…`
or `-F body=@<file>`. The base reaches GitHub through REST only, so without
this its own PRs would pass unread.

Not covered: `PATCH repos/<o>/<r>/issues/<n>` with a body also rewrites a
PR's body, but offline the hook cannot tell a PR's number from an issue's,
and checking every issue edit would block ordinary issues. CI is the gate.

Policy, not a guard (ADR 0005). It fails OPEN on its own errors and on
anything it cannot evaluate without running the command (a body built by
`$(...)`, a body on stdin): CI is the real gate. It blocks (exit 2) only when it
has read the body and `check_body` rejects it.

The message names sections, entries and fields; it never quotes the body.

The project whose IDs the body may cite is the git toplevel of the session's
`cwd`, so a session in a subdirectory still resolves the project's lock; when
git fails or is slow, `cwd` itself (fail open: the worst case is the old
behaviour). A `--body-file` path stays relative to `cwd`, as `gh` reads it.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from trimtab import roots
from trimtab.capture.prblock import check_body
from trimtab.registry.lookup import registry_for

OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", "\n"}
BODY = {"--body", "-b"}
BODY_FILE = {"--body-file", "-F"}
FILL = {"--fill", "--fill-first", "--fill-verbose", "-f"}
UNEVALUATED = ("$(", "`", "${")
GIT_TIMEOUT_SECONDS = 2
# `gh api` options that take a value, so the path is the first token that is neither one nor its value.
API_VALUED = {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input", "-q", "--jq",
              "-t", "--template", "--hostname", "-p", "--preview", "--cache"}
API_RAW_FIELD = {"-f", "--raw-field"}
API_TYPED_FIELD = {"-F", "--field"}
# Matched by shape, not by literal segments: the owner and repository are often
# one shell variable (`repos/$REPO/pulls`) or gh's `{owner}/{repo}`, the number
# a variable too, and gh also takes a full URL.
_REPOS = r"(?:https?://[^/\s]+/)?/?repos/.+"
PULLS = re.compile(_REPOS + r"/pulls/?")
PULL = re.compile(_REPOS + r"/pulls/[^/]+/?")

ALLOW, BLOCK = 0, 2


def project_of(cwd: Path) -> Path:
    """The git toplevel of `cwd`, or `cwd` itself when git fails or times out."""
    try:
        done = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return cwd  # no git, or a hung one: fall back, never block on it
    top = done.stdout.strip()
    return Path(top) if done.returncode == 0 and top else cwd


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments, current = [], []
    for token in lexer:
        if token in OPERATORS or set(token) <= set("&|;()"):
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    return [s for s in segments if s]


def _command(tokens: list[str]) -> list[str]:
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]  # leading VAR=value assignments
    return tokens


def _gh_pr(tokens: list[str]) -> tuple[str, list[str]] | None:
    """('create'|'edit', args) when the segment runs `gh pr create|edit`."""
    tokens = _command(tokens)
    if len(tokens) < 3 or Path(tokens[0]).name != "gh" or tokens[1] != "pr":
        return None
    return (tokens[2], tokens[3:]) if tokens[2] in ("create", "edit") else None


def _api_options(args: list[str]) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    """(method, path, [(option, value)]) for `gh api` arguments.

    `--opt=value` is split, and so is a short option with its value attached
    (`-XPATCH`, `-X=PATCH`, `-fbody=…`), as gh's flag parser accepts them.
    """
    method, path, options = None, None, []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
        elif len(arg) > 2 and arg[:2] in API_VALUED:
            name, eq, value = arg[:2], "=", arg[2:].removeprefix("=")
        else:
            name, eq, value = arg, "", ""
        if name in API_VALUED:
            if not eq:
                if i + 1 >= len(args):
                    break
                i += 1
                value = args[i]
            options.append((name, value))
            if name in ("-X", "--method"):
                method = value.upper()
        elif not arg.startswith("-") and path is None:
            path = arg
        i += 1
    return method, path, options


def _gh_api_pull(tokens: list[str]) -> tuple[str, list[tuple[str, str]]] | None:
    """('create'|'edit', options) when the segment runs `gh api` POST to pulls or PATCH to one pull."""
    tokens = _command(tokens)
    if len(tokens) < 3 or Path(tokens[0]).name != "gh" or tokens[1] != "api":
        return None
    method, path, options = _api_options(tokens[2:])
    if path is None:
        return None
    path = path.split("?", 1)[0]
    if method is None:
        # gh api defaults to GET, and to POST once any field or input is given.
        method = "POST" if any(n in API_RAW_FIELD | API_TYPED_FIELD | {"--input"} for n, _ in options) else "GET"
    if method == "POST" and PULLS.fullmatch(path):
        return "create", options
    if method == "PATCH" and PULL.fullmatch(path):
        return "edit", options
    return None


def _read(cwd: Path, path: str) -> tuple[str, str | None]:
    if path == "-" or any(u in path for u in UNEVALUATED):
        return "allow", "the body comes from stdin or the shell; CI checks it"
    try:
        return "check", (cwd / path).read_text(encoding="utf-8")
    except OSError:
        return "allow", "the body file could not be read; CI checks it"


def api_body_of(action: str, options: list[tuple[str, str]], cwd: Path) -> tuple[str, str | None]:
    """('check', body) | ('block', reason) | ('allow', reason), for the `gh api` form.

    With `--input`, gh sends that file as the request body and any fields as
    query parameters, so the file is read and the fields are not.
    """
    source = next((value for name, value in options if name == "--input"), None)
    if source is not None:
        verdict, text = _read(cwd, source)
        if verdict != "check":
            return verdict, text
        try:
            payload = json.loads(text)
        except ValueError:
            return "allow", "the --input file is not JSON; CI checks it"
        body = payload.get("body") if isinstance(payload, dict) else None
        if isinstance(body, str):
            return "check", body
    else:
        for name, value in options:
            if name not in API_RAW_FIELD | API_TYPED_FIELD:
                continue
            key, eq, field = value.partition("=")
            if key != "body" or not eq:
                continue
            if any(u in field for u in UNEVALUATED):
                return "allow", "the body is built by the shell; CI checks it"
            if name in API_TYPED_FIELD and field.startswith("@"):
                return _read(cwd, field[1:])
            return "check", field
    if action == "create":
        return "block", "no body was given"
    return "allow", "the body is not being changed"


def _option(args: list[str], names: set[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if name.startswith("--") and arg.startswith(name + "="):
                return arg[len(name) + 1:]
    return None


def body_of(action: str, args: list[str], cwd: Path) -> tuple[str, str | None]:
    """('check', body) | ('block', reason) | ('allow', reason)."""
    if "--web" in args or "-w" in args:
        return "allow", "opened in the browser; CI checks it"
    inline, path = _option(args, BODY), _option(args, BODY_FILE)
    if inline is not None:
        if any(u in inline for u in UNEVALUATED):
            return "allow", "the body is built by the shell; CI checks it"
        return "check", inline
    if path is not None:
        return _read(cwd, path)
    if action == "create":
        reason = "--fill takes the body from commits" if FILL & set(args) else "no body was given"
        return "block", reason
    return "allow", "the body is not being changed"


def decide(payload: dict) -> tuple[int, str]:
    if payload.get("tool_name") not in (None, "Bash"):
        return ALLOW, ""
    command = (payload.get("tool_input") or {}).get("command") or ""
    cwd = Path(payload.get("cwd") or ".")
    for tokens in _segments(command):
        porcelain, rest = _gh_pr(tokens), _gh_api_pull(tokens)
        if porcelain is not None:
            verdict, detail = body_of(*porcelain, cwd)
        elif rest is not None:
            verdict, detail = api_body_of(*rest, cwd)
        else:
            continue
        if verdict == "allow":
            continue
        if verdict == "block":
            return BLOCK, (f"pr-body-check: blocked: {detail}. The PR body needs the harness block "
                           "(## Harness items applied, ## Harness feedback, ## Process cost). Write it to a "
                           "file, check it with `bin/trimtab check-pr --body-file <file>`, and pass the file "
                           "(--body-file, or -F body=@<file> with gh api).")
        try:
            instance = roots.instance_root()
        except roots.InstanceError:
            return ALLOW, "pr-body-check: TRIMTAB_INSTANCE is not set; allowing, CI checks the body"
        registry, problems = registry_for(instance, project_of(cwd))
        if problems:
            return ALLOW, "pr-body-check: registry unavailable; allowing, CI checks the body"
        result = check_body(detail, registry)
        if not result.ok:
            lines = "\n".join(f"  {p}" for p in result.problems)
            return BLOCK, (f"pr-body-check: blocked: the PR body's harness block has "
                           f"{len(result.problems)} problem(s):\n{lines}\n"
                           "Fix the body file and check it with `bin/trimtab check-pr --body-file <file>`.")
    return ALLOW, ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        code, message = decide(payload if isinstance(payload, dict) else {})
    except Exception as err:  # fail open: this is policy, CI is the gate (ADR 0005)
        print(f"pr-body-check: internal error ({type(err).__name__}); allowing, CI checks the body",
              file=sys.stderr)
        return ALLOW
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
