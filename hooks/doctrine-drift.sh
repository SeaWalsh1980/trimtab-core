#!/usr/bin/env bash
# SessionStart hook, matcher: *. NOT REGISTERED: registering it is control
# change C2 (plan 0009, section 3b), which needs the operator's authorization.
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
PYTHONPATH="$here/.." python3 -P -m trimtab.adapters.doctrine_drift <<<"$payload"
exit 0
