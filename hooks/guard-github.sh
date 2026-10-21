#!/usr/bin/env bash
# PreToolUse hook, matcher: Bash. Registered in settings.base.json; changing
# that registration is a security-control change (ADR 0011).
#
# Refuses the GitHub writes the operator owns: a PR opened as anything but a
# draft, an approving review, resolving a review thread, and API writes to the
# default branch (and `gh repo sync --force`). The rules, the spellings read
# and the residuals are in trimtab/adapters/guard_github.py and
# docs/adr/0037-guard-github-refuses-the-github-writes-the-operator-owns.md.
# Exit 2 = blocked, with a message that names the rule and never quotes the
# command.
#
# Fails CLOSED (ADR 0001): an empty payload, a payload that is not a JSON
# object, a missing or too-old python3, a missing loader or an adapter error
# all block. The bash prefilter below decides only that a command cannot be
# about GitHub, and a payload that hides its text from it (a JSON \u escape,
# ANSI-C quoting) goes to the adapter.
#
# Fast path: python3 starts only for a command naming gh, graphql or a
# github.com host once quotes and backslashes are removed. Anything else exits
# after one bash startup.
#
# Session override: export HOOK_ALLOW_GITHUB=1
#   (must be exported before launching claude — an inline prefix on a Bash
#    command never reaches this process). The operator's decision only.

set -uo pipefail   # deliberately not -e: an unexpected non-zero must not
                   # short-circuit into a silent pass

[[ "${HOOK_ALLOW_GITHUB:-0}" == "1" ]] && exit 0

block() {
  echo "guard-github: blocked: $1" >&2
  exit 2
}

payload=$(cat) || block "the hook payload could not be read"
[[ -n "$payload" ]] || block "empty hook payload"
trimmed="${payload#"${payload%%[![:space:]]*}"}"
[[ "$trimmed" == "{"* ]] || block "the hook payload is not a JSON object"

# JSON's whitespace escapes (\n, \t, …) become spaces, so a command on its own
# line still starts a word; then quotes and backslashes go, so g"h" and g\h
# read as gh. A \u escape or $'…' can spell any letter, so either sends the
# payload to the adapter.
flat=${payload//\\[nrtbf]/ }
flat=${flat//[\"\'\\]/}
about_github='(^|[^A-Za-z0-9_.-])gh([^A-Za-z0-9_.-]|$)|graphql|github\.com'
if [[ "$payload" != *'\u'* && "$payload" != *"\$'"* ]]; then
  shopt -s nocasematch
  [[ "$flat" =~ $about_github ]] || exit 0
  shopt -u nocasematch
fi

command -v python3 >/dev/null 2>&1 || block "python3 is not available, so a command naming GitHub cannot be read"
# -P exists only from 3.11, so the probe is also the version check.
python3 -P -I -S -c '' >/dev/null 2>&1 || block "python3 3.11 or later is not available"

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
[[ -r "$here/run_module.py" ]] || block "hooks/run_module.py is missing"
# -I -S: no working directory (a trimtab/ there cannot shadow this checkout's
# package), no site-packages of any kind, no PYTHON* variables. run_module.py
# loads the package without putting the root on sys.path, so no file at the
# root can stand in for a module (ADR 0035). -B: write no bytecode beside the
# package.
python3 -I -S -B "$here/run_module.py" "$here/.." trimtab.adapters.guard_github <<<"$payload"
rc=$?
[[ $rc -eq 0 ]] && exit 0
# The adapter printed its reason; any other non-zero is a fault, and blocks.
[[ $rc -eq 2 ]] || echo "guard-github: blocked: the adapter failed (exit $rc)" >&2
exit 2
