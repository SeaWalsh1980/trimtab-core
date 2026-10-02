#!/usr/bin/env bash
# PreToolUse hook, matcher: Bash. NOT REGISTERED: registering it is control
# change C2 (plan 0009, section 3b), which needs the operator's authorization.
#
# Checks the body of `gh pr create` / `gh pr edit --body*` for the harness
# block (plan section 3a) before the PR is opened. Exit 2 = blocked, with a
# message that names sections and fields and never quotes the body.
#
# Unlike the four guards, this is policy, not a security control, and it fails
# OPEN: on its own errors, a missing python3, or a body it cannot read without
# running the command, it allows. CI is the gate.
#
# Fast path: a command that does not contain `gh pr` exits before python3
# starts, so the cost on ordinary Bash calls is one bash startup (risk R4;
# latencies in docs/adr/0009).

payload=$(cat) || exit 0
[[ "$payload" =~ gh[[:space:]]+pr ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
PYTHONPATH="$here/.." python3 -m trimtab.adapters.pr_body_check <<<"$payload"
rc=$?
[[ $rc -eq 2 ]] && exit 2
exit 0
