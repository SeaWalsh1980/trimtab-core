#!/usr/bin/env bash
# PreToolUse hook, matcher: Bash. Registered in settings.base.json; changing
# that registration is a security-control change (ADR 0004).
#
# Checks the body of `gh pr create` / `gh pr edit --body*` for the harness
# block (ADR 0005) before the PR is opened. Exit 2 = blocked, with a
# message that names sections and fields and never quotes the body.
#
# Unlike the four guards, this is policy, not a security control, and it fails
# OPEN: on its own errors, a missing python3, or a body it cannot read without
# running the command, it allows. CI is the gate.
#
# Fast path: a command that does not contain `gh pr` exits before python3
# starts, so the cost on ordinary Bash calls is one bash startup (latencies
# in docs/adr/0005-harness-items-by-id-and-the-evidence-loop.md).

payload=$(cat) || exit 0
[[ "$payload" =~ gh[[:space:]]+pr ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
python3 -P -c '' >/dev/null 2>&1 || exit 0   # an interpreter without -P: fail open, CI is the gate

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# -P: never put the working directory on sys.path, so a trimtab/ directory
# there cannot shadow this checkout's package (it would run instead, silently).
PYTHONPATH="$here/.." python3 -P -m trimtab.adapters.pr_body_check <<<"$payload"
rc=$?
[[ $rc -eq 2 ]] && exit 2
exit 0
