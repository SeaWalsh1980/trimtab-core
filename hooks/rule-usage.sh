#!/usr/bin/env bash
# Observability hook for InstructionsLoaded / SessionStart / SubagentStart.
# All of the behaviour is in rule_usage.py beside this file; see its docstring.
#
# Thin on purpose. The payload arrives on stdin, so the Python cannot be an
# inline heredoc the way bootstrap.sh does it — the heredoc would consume the
# very stdin the hook is here to read. exec lets stdin through untouched and
# costs no extra process.
#
# Unlike the guard hooks, this one cannot fail closed: Claude Code treats an
# observability hook's exit code as non-blocking whatever it is. A missing
# python3 is therefore reported on stderr, which is the only channel left, and
# never blocks the session that tripped it.
set -uo pipefail

dir="$(dirname "${BASH_SOURCE[0]}")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "rule-usage: python3 not found — the rule-usage observer is recording nothing" >&2
  exit 1
fi

# -B: write no bytecode beside the script; a snapshot is never written.
# -I -S: no script or working directory on sys.path, no site-packages of any
# kind (so no .pth the session wrote), no PYTHON* variables. The observer runs
# on every tool call, so code that ran inside it could write guard-state, which
# guard-paths keeps the session itself away from (ADR 0035). It needs nothing
# beyond the standard library.
exec python3 -I -S -B "$dir/rule_usage.py"
