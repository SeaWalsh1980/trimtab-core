#!/usr/bin/env bash
# Bash write detector: PreToolUse (`pre`), PostToolUse and PostToolUseFailure
# (`post`), matcher: Bash. Registered in settings.base.json; changing that
# registration is a security-control change (SEC-7).
#
# guard-paths judges the Edit-family tools against the write allowlist and,
# in an autonomous plan run, the plan's scope; it cannot judge a shell write,
# whose commands cannot be listed (ADR 0039). This hook judges what a Bash
# call changed instead: `pre` snapshots the repository's changed paths, and
# `post` judges every path the call changed by the same layers. See
# docs/adr/0065-a-detector-judges-bash-writes-against-the-write-allowlist.md
# and trimtab/adapters/bash_writes.py.
#
# It detects, it cannot prevent: by `post` the command has run. Exit 2 from
# `post` puts the refusal in front of the agent; exit 2 from `pre` blocks the
# call when no snapshot could be recorded, since its writes could not be
# judged. PostToolUseFailure is registered too: a command that exits non-zero
# raises that event, not PostToolUse, and would otherwise go unjudged.
#
# Fails closed: a missing python3 3.11, loader or classifier blocks (`pre`)
# or reports the call unjudged (`post`). The operator lifts it, with
# guard-paths' harness layers, by exporting HOOK_ALLOW_HARNESS=1 before
# launching claude; an inline prefix on a Bash command never reaches a hook.

phase="${1:-}"
[[ "$phase" == pre || "$phase" == post ]] || { echo "bash-writes: usage: bash-writes.sh pre|post" >&2; exit 2; }
[[ "${HOOK_ALLOW_HARNESS:-0}" == "1" ]] && exit 0

payload=$(cat) || { echo "bash-writes: the hook payload could not be read; this call was not judged" >&2; exit 2; }
# The adapter checks the version itself (3.11 or later): a separate probe
# would cost one more interpreter start on every Bash call (ADR 0065).
if ! command -v python3 >/dev/null 2>&1; then
  echo "bash-writes: python3 is not available, so Bash writes cannot be judged" >&2
  exit 2
fi
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
[[ -r "$here/run_module.py" ]] || { echo "bash-writes: hooks/run_module.py is missing; this call was not judged" >&2; exit 2; }

if [[ "$phase" == pre ]]; then
  # -I -S -B as in pr-monitor-record.sh (ADR 0035): no working directory, no
  # site-packages, no PYTHON* variables, no bytecode beside the package.
  python3 -I -S -B "$here/run_module.py" "$here/.." trimtab.adapters.bash_writes snapshot <<<"$payload"
  rc=$?
  (( rc == 0 )) && exit 0
  (( rc == 2 )) || echo "bash-writes: the snapshot adapter failed (exit $rc); this call was not judged" >&2
  exit 2
fi

# The mode comes from the classifier guard-paths uses, so the two cannot
# disagree (ADR 0041). Without it no write can be judged.
# shellcheck source=hooks/session-mode.sh
if ! . "$here/session-mode.sh" 2>/dev/null || ! declare -F sm_classify >/dev/null; then
  echo "bash-writes: the session-mode classifier (hooks/session-mode.sh) is missing; this call's writes were not judged" >&2
  exit 2
fi
sm_payload <<<"$payload"
cwd="$sm_cwd"
[[ -n "$cwd" && -d "$cwd" ]] || cwd="$PWD"
sm_classify "$cwd" "$sm_sid"
python3 -I -S -B "$here/run_module.py" "$here/.." trimtab.adapters.bash_writes judge \
  "$sm_mode" "${sm_branch_name:-}" <<<"$payload"
rc=$?
(( rc == 0 )) && exit 0
(( rc == 2 )) || echo "bash-writes: the judging adapter failed (exit $rc); this call's writes were not judged" >&2
exit 2
