"""PreToolUse adapter: check the PR body a `gh pr create|edit` command would send.

Policy, not a guard (plan section 3a). It fails OPEN on its own errors and on
anything it cannot evaluate without running the command (a body built by
`$(...)`, a body on stdin): CI is the real gate. It blocks (exit 2) only when it
has read the body and `check_body` rejects it.

The message names sections, entries and fields; it never quotes the body.
"""

from __future__ import annotations

import json
import shlex
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

ALLOW, BLOCK = 0, 2


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


def _gh_pr(tokens: list[str]) -> tuple[str, list[str]] | None:
    """('create'|'edit', args) when the segment runs `gh pr create|edit`."""
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]  # leading VAR=value assignments
    if len(tokens) < 3 or Path(tokens[0]).name != "gh" or tokens[1] != "pr":
        return None
    return (tokens[2], tokens[3:]) if tokens[2] in ("create", "edit") else None


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
        if path == "-" or any(u in path for u in UNEVALUATED):
            return "allow", "the body comes from stdin or the shell; CI checks it"
        try:
            return "check", (cwd / path).read_text(encoding="utf-8")
        except OSError:
            return "allow", "the body file could not be read; CI checks it"
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
        found = _gh_pr(tokens)
        if found is None:
            continue
        verdict, detail = body_of(*found, cwd)
        if verdict == "allow":
            continue
        if verdict == "block":
            return BLOCK, (f"pr-body-check: blocked: {detail}. The PR body needs the harness block "
                           "(## Harness items applied, ## Harness feedback, ## Process cost). Write it to a "
                           "file, check it with `bin/trimtab check-pr --body-file <file>`, and pass --body-file.")
        try:
            instance = roots.instance_root()
        except roots.InstanceError:
            return ALLOW, "pr-body-check: TRIMTAB_INSTANCE is not set; allowing, CI checks the body"
        registry, problems = registry_for(instance, cwd)
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
    except Exception as err:  # fail open: this is policy, CI is the gate (plan section 3a)
        print(f"pr-body-check: internal error ({type(err).__name__}); allowing, CI checks the body",
              file=sys.stderr)
        return ALLOW
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
