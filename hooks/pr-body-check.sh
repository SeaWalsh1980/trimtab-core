#!/usr/bin/env bash
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

payload=$(cat) || exit 0
porcelain='gh[[:space:]]+pr'
rest='gh[[:space:]]+api.*pulls'
write='(-X|--method|-f|-F|--raw-field|--field|--input)'
if ! [[ "$payload" =~ $porcelain ]]; then
  [[ "$payload" =~ $rest && "$payload" =~ $write ]] || exit 0
fi
command -v python3 >/dev/null 2>&1 || exit 0
# -P exists only from 3.11: an older interpreter, or one that rejects -I, fails
# this probe, and the check fails open (CI is the gate).
python3 -P -I -c '' >/dev/null 2>&1 || exit 0

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# -I: no working directory (a trimtab/ there cannot shadow this checkout's
# package), no user site-packages (so no .pth the session wrote), no PYTHON*
# variables. The package root goes on sys.path after the standard library, so
# no sitecustomize.py or standard-library name at the root can stand in for it
# (ADR 0035). No -S: the check needs PyYAML from system site-packages.
# -B: write no bytecode beside the package; a snapshot is never written.
run_module='import runpy, sys; sys.path.append(sys.argv[1]); del sys.argv[:2]; runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)'
python3 -I -B -c "$run_module" "$here/.." trimtab.adapters.pr_body_check <<<"$payload"
rc=$?
[[ $rc -eq 2 ]] && exit 2
exit 0
