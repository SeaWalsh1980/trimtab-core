#!/usr/bin/env bash
# What the weekly timer runs, and what to run by hand to reproduce it.
#
#   rule-usage-weekly.sh [--days N]
#
# Writes ~/.claude/logs/rule-usage-<ISO week>.md and prints the path. The
# report is dated by ISO week so a re-run inside the same week overwrites its
# own file rather than accumulating near-duplicates.
#
# Exit codes are deliberately not a straight pass-through of the report's:
#
#   report 0  nothing needs a person          -> 0, the unit succeeds
#   report 1  the report carries warnings     -> 0, because the warnings are
#             IN the file the operator reads; failing the unit as well would
#             train them to ignore a failed unit
#   report 2  no report could be produced     -> 2, the unit fails and shows
#             up in `systemctl --user status`, which is the only signal left
#             when there is no report to read
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
logdir="${CLAUDE_RULE_USAGE_DIR:-$HOME/.claude/logs}"
days=7
[[ "${1:-}" == "--days" ]] && days="${2:?--days needs a number}"

out="$logdir/rule-usage-$(date -u +%G-W%V).md"
python3 "$here/rule-usage-report.py" --days "$days" --out "$out"
rc=$?

case $rc in
  0|1) exit 0 ;;
  *)   exit "$rc" ;;
esac
