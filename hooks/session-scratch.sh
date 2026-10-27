#!/usr/bin/env bash
# Session scratch: an autonomous plan run keeps its temporary files in one
# directory per session, and that directory goes when the session ends. See
# docs/adr/0049-session-end-removes-an-autonomous-runs-scratch.md.
#
#   session-scratch.sh start   SessionStart. For a run, create
#                              $TMPDIR/trimtab-run-<uid>/<session id> and tell
#                              the session to keep temporary files there, or in
#                              the scratchpad its harness gives it.
#   session-scratch.sh end     SessionEnd. For a run, remove that directory and
#                              the harness's scratchpad for this session,
#                              $TMPDIR/claude-<uid>/<project>/<session id>/scratchpad.
#
# $TMPDIR is /tmp when unset or not absolute; the harness takes its temp
# directory from the same variable.
#
# Interactive sessions are left alone: a person may want their scratch kept.
# The mode is hooks/session-mode.sh's, so a session that was a run at any
# point is still one at its end.
#
# The harness's scratchpad layout is not documented, so it is removed only
# when every directory on its path is real (no link) and owned by this user,
# and the project directory's name has the harness's leading `-`. A changed
# layout matches nothing, and nothing is removed. The session id must be a
# plain token, so it cannot name a parent directory. Caches and other
# sessions' files are never touched: removing more is riskier than leaving it.
#
# Never blocks and always exits 0: neither event is a gate. SessionEnd has no
# one to tell, so a removal that fails is reported on stderr, which Claude
# Code shows in its debug log, and the directory stays for the operator.
set -uo pipefail

ss_here="${BASH_SOURCE[0]%/*}"
[[ "$ss_here" == "${BASH_SOURCE[0]}" ]] && ss_here=.
# shellcheck source=hooks/session-mode.sh
. "$ss_here/session-mode.sh" 2>/dev/null && declare -F sm_classify >/dev/null || exit 0

ss_tmp="${TMPDIR:-/tmp}"
[[ "$ss_tmp" == /* ]] || ss_tmp=/tmp
ss_tmp="${ss_tmp%/}"
[[ -n "$ss_tmp" ]] || ss_tmp=/tmp
ss_base="$ss_tmp/trimtab-run-$UID"

ss_own() { [[ -d "$1" && ! -L "$1" && -O "$1" ]]; }

ss_remove() {   # $1 = a directory already checked by ss_own
  rm -rf -- "$1" 2>/dev/null || printf 'session-scratch: could not remove %s\n' "$1" >&2
}

ss_json() {   # $1 = text; prints it escaped for a JSON string
  local s="$1"
  s=${s//\\/\\\\}; s=${s//\"/\\\"}
  printf '%s' "$s"
}

ss_start() {
  local dir="$ss_base/$sm_sid" ctx
  # A base made earlier with wider access is narrowed before it is used.
  ( umask 077 && mkdir -p "$ss_base" ) 2>/dev/null
  ss_own "$ss_base" && chmod 700 "$ss_base" 2>/dev/null \
    && ( umask 077 && { [[ -d "$dir" ]] || mkdir "$dir"; } ) 2>/dev/null
  if ss_own "$ss_base" && ss_own "$dir"; then
    ctx="Session scratch: keep temporary files only in $dir or in the scratchpad your harness gives you."
    ctx+=" $dir is removed when this session ends, and so is the harness scratchpad where its layout is"
    ctx+=" the one this hook knows; name anything left elsewhere in the progress record."
  else
    ctx="Session scratch: could not create a private directory at $dir. Keep temporary files only in the"
    ctx+=" scratchpad your harness gives you; where it gives none, name what you leave behind in the progress record."
  fi
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$(ss_json "$ctx")"
}

ss_end() {
  local root="$ss_tmp/claude-$UID" project pad
  if ss_own "$ss_base" && ss_own "$ss_base/$sm_sid"; then
    ss_remove "$ss_base/$sm_sid"
  fi
  ss_own "$root" || return 0
  shopt -s nullglob
  for project in "$root"/-*; do
    pad="$project/$sm_sid/scratchpad"
    ss_own "$project" && ss_own "$project/$sm_sid" && ss_own "$pad" && ss_remove "$pad"
  done
}

ss_main() {
  local event="${1:-}" cwd
  # Any other event has nothing to do, so it costs neither the payload read
  # nor the classifier's git calls.
  [[ "$event" == start || "$event" == end ]] || return 0
  sm_payload
  sm_valid_sid "$sm_sid" || return 0
  cwd="$sm_cwd"
  [[ -n "$cwd" && -d "$cwd" ]] || cwd="$PWD"
  # Only the start records the session; at its end there is nothing left to
  # tighten, and a record would outlive the session for the full TTL.
  sm_classify "$cwd" "$sm_sid" "$([[ "$event" == start ]] && echo 1 || echo 0)"
  [[ "$sm_mode" == autonomous ]] || return 0
  case "$event" in
    start) ss_start ;;
    end) ss_end ;;
  esac
}

ss_main "${1:-}"
exit 0
