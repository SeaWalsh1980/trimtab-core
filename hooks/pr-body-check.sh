#!/bin/bash
# PreToolUse hook, matcher: Bash. Registered in settings.base.json; changing
# that registration is a security-control change (ADR 0004).
#
# Checks the body of `gh pr create` / `gh pr edit --body*`, and of their REST
# form (`gh api` POST to `…/pulls`, PATCH to `…/pulls/<n>`), for the harness
# block (ADR 0005) before the PR is opened. Exit 2 = blocked, with a
# message that names sections and fields and never quotes the body.
#
# Unlike the four guards, this checks policy, not security, and it fails
# OPEN: on its own errors, a missing python3, or a body it cannot read without
# running the command, it allows. CI is the gate.
#
# Fast path: python3 starts only for a command holding `gh pr`, or a `gh api`
# call naming `pulls` together with a sign of a write (a method flag, a field
# or `--input`). Anything else, a plain read of pulls included, exits after
# one bash startup (latencies in
# docs/adr/0005-harness-items-by-id-and-the-evidence-loop.md). The adapter
# decides the rest: a read, or another endpoint, passes there.

# Before any command: tools are named bare, and the inherited PATH starts with
# a user-writable directory (docs/adr/0065-hooks-reset-path-first.md).
export PATH=/usr/bin:/bin
# Failing open is by design, never silent: each fault that stops the check
# tells the user that the body went unchecked and why, here for the hook's own
# faults and in the adapter for its own. Claude Code shows a hook's stderr on
# exit 0 only in its debug log, so the notice is a systemMessage on stdout, as
# session-path.sh does, and a copy goes to stderr. A body the check never meant
# to read (--web, stdin, a shell-built body) stays quiet: CI checks those.
# Since the reset above, python3 is always /usr/bin/python3, whatever newer one
# the session's PATH holds (ADR 0065).
unchecked() { # unchecked <why>
  local msg="pr-body-check: the PR body was not checked: $1. CI is the gate."
  echo "$msg" >&2
  # JSON string escapes: backslash first, then quote and the control
  # characters a path could hold.
  msg=${msg//\\/\\\\}; msg=${msg//\"/\\\"}
  msg=${msg//$'\n'/\\n}; msg=${msg//$'\r'/\\r}; msg=${msg//$'\t'/\\t}
  printf '{"systemMessage":"%s"}\n' "$msg"
  exit 0
}
payload=$(cat) || unchecked "the hook payload could not be read"
porcelain='gh[[:space:]]+pr'
rest='gh[[:space:]]+api.*pulls'
write='(-X|--method|-f|-F|--raw-field|--field|--input)'
if ! [[ "$payload" =~ $porcelain ]]; then
  [[ "$payload" =~ $rest && "$payload" =~ $write ]] || exit 0
fi
command -v python3 >/dev/null 2>&1 || unchecked "no python3 in /usr/bin or /bin"
# -P exists only from 3.11: an older interpreter, or one that rejects -I, fails
# this probe, and the check fails open (CI is the gate).
python3 -P -I -c '' >/dev/null 2>&1 \
  || unchecked "$(command -v python3) is older than 3.11 or rejects -P -I"

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# python3 exits 2 when it cannot open its script, which would read as a block
# below: a missing loader fails open like every other fault here.
[[ -r "$here/run_module.py" ]] || unchecked "its loader run_module.py is missing"
# -I: no working directory (a trimtab/ there cannot shadow this checkout's
# package), no user site-packages (so no .pth the session wrote), no PYTHON*
# variables. No -S: the check needs PyYAML from system site-packages.
# run_module.py loads the package from the checkout without putting the root
# on sys.path, so no file at the root, and no trimtab package installed in
# site-packages, can stand in for a module (ADR 0035).
# -B: write no bytecode beside the package; a snapshot is never written.
# Its stdout is held, so that on a failed run only this hook's notice reaches
# Claude Code, never half of the adapter's output before it.
out=$(python3 -I -B "$here/run_module.py" "$here/.." trimtab.adapters.pr_body_check <<<"$payload")
rc=$?
[[ $rc -eq 2 ]] && exit 2
# Any other non-zero is the check failing: the loader refusing, or the adapter
# crashing (a PyYAML missing from this python3 is one cause). Its own stderr
# names which.
[[ $rc -eq 0 ]] || unchecked "the check exited $rc under $(command -v python3); its stderr says why"
[[ -n "$out" ]] && printf '%s\n' "$out"
exit 0
