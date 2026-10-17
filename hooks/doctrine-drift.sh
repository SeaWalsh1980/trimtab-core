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
# -I -S: no working directory (a trimtab/ there cannot shadow this checkout's
# package), no site-packages of any kind (so no .pth and no sitecustomize), no
# PYTHON* variables. The package root goes on sys.path after the standard
# library, so nothing at the root can stand in for it (ADR 0035).
# -B: write no bytecode beside the package; a snapshot is never written.
run_module='import runpy, sys; sys.path.append(sys.argv[1]); del sys.argv[:2]; runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)'
python3 -I -S -B -c "$run_module" "$here/.." trimtab.adapters.doctrine_drift <<<"$payload"
exit 0
