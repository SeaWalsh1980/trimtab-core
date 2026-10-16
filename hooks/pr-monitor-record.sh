#!/usr/bin/env bash
# PostToolUse hook, matcher: Bash. Registered in settings.base.json; changing
# that registration is a security-control change (ADR 0011).
#
# Records each PR the session itself creates, so guard-mcp can pass
# mcp__ccd_pr__set_monitor for that PR and deny it for any other. The rules for
# what counts as a create, and where the list lives, are in
# docs/adr/0028-guard-mcp-passes-set-monitor-only-for-prs-the-session-opened.md
# and trimtab/adapters/pr_monitor.py.
#
# Writes nothing to stdout. Exits 0, or 1 with one line on stderr when a create
# was seen and could not be recorded. Never 2: a PostToolUse hook has nothing to
# block. No output names a URL or a session.
#
# Fails closed by doing nothing: when this hook, python3 or the list is
# missing, nothing is recorded and guard-mcp denies set_monitor.
#
# Fast path: python3 starts only for a payload holding `gh pr create` or
# `gh api`. Anything else exits after one bash startup.

payload=$(cat) || exit 1
create='gh[[:space:]]+(pr[[:space:]]+create|api)'
[[ "$payload" =~ $create ]] || exit 0
if ! command -v python3 >/dev/null 2>&1 || ! python3 -P -c '' >/dev/null 2>&1; then
  echo "pr-monitor: python3 3.11 or later is not available; a PR create was not recorded" >&2
  exit 1
fi

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# -P: never put the working directory on sys.path, so a trimtab/ directory
# there cannot shadow this checkout's package. -B: write no bytecode beside the
# package; a snapshot is never written.
PYTHONPATH="$here/.." python3 -P -B -m trimtab.adapters.pr_monitor record <<<"$payload" >/dev/null
[[ $? -eq 0 ]] && exit 0
exit 1
