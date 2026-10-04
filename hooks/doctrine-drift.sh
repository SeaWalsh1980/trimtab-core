#!/usr/bin/env bash
# SessionStart hook, matcher: *. Registered in settings.base.json; changing
# that registration is a security-control change (ADR 0004).
#
# Warns when the Trimtab checkout this hook runs from is not the SHA the
# project's .claude/trimtab.json was reviewed against, or when the project's
# scaffold schema is behind. Silent when they match, when the project has not
# adopted Trimtab, and in Trimtab's own checkouts. SessionStart cannot block,
# so it always exits 0, including on malformed input.

payload=$(cat) || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# -P: never put the working directory on sys.path, so a trimtab/ directory
# there cannot shadow this checkout's package (it would run instead, silently).
# -B: write no bytecode beside the package; a snapshot is never written.
PYTHONPATH="$here/.." python3 -P -B -m trimtab.adapters.doctrine_drift <<<"$payload"
exit 0
