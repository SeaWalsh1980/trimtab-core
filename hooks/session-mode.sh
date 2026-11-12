#!/bin/bash
# Session mode: is this session interactive, or an autonomous plan run?
#
# Two kinds of session exist. In an interactive one a person is in the loop.
# An autonomous plan run is automation with the person taken out, working
# through a plan the admin merged; it gets the stricter guardrails, and it may
# never do more than an interactive session could. See
# docs/adr/0041-session-modes-and-plan-scoped-writes.md.
#
# This file is both:
#   - a SessionStart hook (registered in settings.base.json), which announces
#     the mode in the session's context so the session works by it from its
#     first turn; and
#   - a library guard-paths sources, so the mode the session is told and the
#     mode the guard enforces come from one function and cannot disagree.
#
# The test fails toward strict, since a run taken for an interactive session
# would get the looser rules. A session is autonomous when ANY signal holds:
#   1. TRIMTAB_PLAN_RUN=1 in this process's environment. The operator sets it
#      in the cloud environment or exports it before a local run; an inline
#      prefix on a Bash command never reaches a hook.
#   2. The repository is on a `plan/*` branch. Plan work happens only there,
#      so this catches a run whose variable was forgotten.
#   3. This session was recorded as autonomous earlier. The mode only
#      tightens: once a signal held, the session stays autonomous, even after
#      it leaves the plan branch, and in every repository. The record lives
#      under the config directory's guard-state/, where guard-paths refuses
#      every write by a tool (reads are allowed; the records are empty).
# Interactive only when none holds.
#
# A record is per session id, and a clear (/clear, or a parent clearing its
# worker) starts a new one. So a run must not rely on signals 2 and 3 alone:
# every autonomous run, local as well as cloud, is launched with
# TRIMTAB_PLAN_RUN=1, which no clear removes.
set -uo pipefail

sm_git() {   # $1 = a directory in the repository, then git's arguments
  local dir="$1"
  shift
  # The caller's repository-steering variables are dropped so the hook reads
  # the repository the path is in, not one an environment points at.
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
      -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES \
      -u GIT_COMMON_DIR -u GIT_CEILING_DIRECTORIES \
      GIT_NO_REPLACE_OBJECTS=1 GIT_LITERAL_PATHSPECS=1 \
      git -C "$dir" -c core.fsmonitor=false "$@"
}

sm_branch() {   # $1 = a directory; prints the branch name, or nothing
  # The full ref, not --short: --short prints `heads/plan/...` when a tag of
  # the same name exists, which would hide the plan branch from signal 2.
  local ref
  ref=$(sm_git "$1" symbolic-ref -q HEAD 2>/dev/null) || return 0
  [[ "$ref" == refs/heads/* ]] && printf '%s' "${ref#refs/heads/}"
}

# Where the per-session records live; only hooks write them. Set once, with
# no subshell: this runs on every write.
sm_dir=""
if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then sm_dir="${CLAUDE_CONFIG_DIR%/}/guard-state/session-mode"
elif [[ -n "${HOME:-}" ]]; then sm_dir="${HOME%/}/.claude/guard-state/session-mode"
fi

# Records follow trimtab/adapters/pr_monitor.py's convention (ADR 0028): the
# session id must be a plain token, the file is named by its sha256 so no
# state path names a session, and records older than the same two-day TTL are
# pruned. A run refreshes its own record on every write, so only a session
# idle that long loses its record.
sm_ttl_minutes=2880
sm_valid_sid() { [[ "$1" =~ ^[A-Za-z0-9_-]{1,128}$ ]]; }

sm_key_for="" sm_key=""
sm_record_name() {   # $1 = session id; sets sm_key, or returns 1
  [[ -n "$sm_key_for" && "$sm_key_for" == "$1" ]] && return 0
  local h _
  sm_key="" sm_key_for=""
  sm_valid_sid "$1" || return 1
  if command -v sha256sum >/dev/null 2>&1; then
    read -r h _ < <(printf '%s' "$1" | sha256sum 2>/dev/null)
  elif command -v shasum >/dev/null 2>&1; then
    read -r h _ < <(printf '%s' "$1" | shasum -a 256 2>/dev/null)
  fi
  # No hash tool: no record can be named, so the session keeps its other
  # signals but loses stickiness.
  [[ "${h:-}" =~ ^[0-9a-f]{64}$ ]] || return 1
  sm_key="$h" sm_key_for="$1"
}

sm_recorded() {   # $1 = session id; 0 = recorded autonomous (or cannot tell)
  local dir="$sm_dir"
  sm_valid_sid "$1" || return 1
  [[ -n "$dir" ]] || return 1
  # A link anywhere on the record's path could point the record somewhere a
  # session can delete it. Treat that as recorded: strict, never looser.
  [[ -L "${dir%/*}" || -L "$dir" ]] && return 0
  # No record directory, no record: skip hashing (an exec) on hosts that have
  # never recorded a run.
  [[ -d "$dir" ]] || return 1
  sm_record_name "$1" || return 1
  [[ -L "$dir/$sm_key" ]] && return 0
  [[ -e "$dir/$sm_key" ]]
}

sm_record() {   # $1 = session id; best effort, a failure only loses stickiness
  local dir="$sm_dir" new=0
  [[ -n "$dir" ]] || return 0
  sm_record_name "$1" || return 0
  [[ -L "${dir%/*}" || -L "$dir" || -L "$dir/$sm_key" ]] && return 0
  [[ -e "$dir/$sm_key" ]] || new=1
  # `: >` refreshes the record's age with no exec.
  ( umask 077 && mkdir -p "$dir" && : > "$dir/$sm_key" ) 2>/dev/null || return 0
  # Pruning runs once per session, when its record is created: other
  # sessions' records past the TTL, and any planted link.
  if (( new )); then
    find "$dir" -mindepth 1 -maxdepth 1 \( -type l -o \( -type f -mmin "+$sm_ttl_minutes" \) \) \
      ! -name "$sm_key" -delete 2>/dev/null
  fi
  return 0
}

# sm_classify <directory> <session id> [record: 1|0]
# Sets sm_mode (interactive|autonomous), sm_signals (why) and sm_branch_name.
# Records the session when it is autonomous, so the mode only tightens,
# unless record is 0 (a session that is ending has nothing left to tighten).
sm_classify() {
  local dir="$1" sid="${2:-}" record="${3:-1}"
  local -a why=()
  sm_branch_name=$(sm_branch "$dir")
  [[ "${TRIMTAB_PLAN_RUN:-}" == "1" ]] && why+=("TRIMTAB_PLAN_RUN=1")
  [[ "$sm_branch_name" == plan/* ]] && why+=("branch $sm_branch_name")
  sm_recorded "$sid" && why+=("recorded autonomous earlier in this session")
  if (( ${#why[@]} )); then
    sm_mode=autonomous
    printf -v sm_signals '%s; ' "${why[@]}"
    sm_signals=${sm_signals%; }
    [[ "$record" == 0 ]] || sm_record "$sid"
  else
    sm_mode=interactive
    sm_signals="no autonomous-run signal"
  fi
}

# sm_payload: reads a session hook's payload on stdin and sets sm_cwd and
# sm_sid, each "" when absent or unreadable. hooks/session-scratch.sh reads
# its payload the same way.
sm_payload() {
  local payload cwd="" sid=""
  payload=$(cat 2>/dev/null) || payload=""
  if command -v jq >/dev/null 2>&1; then
    cwd=$(printf '%s' "$payload" | jq -r '(.cwd // "") | tostring' 2>/dev/null) || cwd=""
    sid=$(printf '%s' "$payload" | jq -r '(.session_id // "") | tostring' 2>/dev/null) || sid=""
  elif command -v python3 >/dev/null 2>&1; then
    { IFS= read -r cwd; IFS= read -r sid; } < <(printf '%s' "$payload" | python3 -I -X pycache_prefix=/dev/null/no-pycache -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = {}
d = d if isinstance(d, dict) else {}
for k in ("cwd", "session_id"):
    v = d.get(k)
    print(v.replace("\n", " ") if isinstance(v, str) else "")
' 2>/dev/null)
  fi
  sm_cwd="$cwd" sm_sid="$sid"
}

# The recorder functions below set PATH=/usr/bin:/bin for themselves, so no tool
# they run (readlink, find, rm, ...) comes from a directory the session can write
# (~/.local/bin). guard-paths sources this file as a library, so the PATH change
# is local to each function, never global.
SM_SYSTEM_PATH=/usr/bin:/bin
# A path no .pyc can be under: Python then reads no bytecode beside the code it
# runs, so a planted __pycache__ cannot stand in for a module (-B stops writes only).
SM_NO_PYCACHE=/dev/null/no-pycache

# sm_trusted <name>: the absolute path of a system binary that only root can
# change, or nothing. What runs is where the name resolves, so that file and its
# directory are judged (a link's own mode is always 0777, so the link itself is
# not), and the link's directory too, since whoever can write it can re-point it.
SM_TRUSTED_DIRS="/usr/bin /bin"  # set here, never read from the environment
sm_trusted() {
  local PATH="$SM_SYSTEM_PATH" d p real x
  for d in $SM_TRUSTED_DIRS; do
    p="$d/$1"
    [[ -f "$p" && -x "$p" ]] || continue
    real=$(readlink -f -- "$p") || return 1
    [[ -f "$real" ]] || return 1
    for x in "$d" "$real" "${real%/*}"; do
      [[ -n $(find "$x" -maxdepth 0 -user 0 ! -perm /022 2>/dev/null) ]] || return 1
    done
    printf '%s\n' "$p"
    return 0
  done
  return 1
}

# sm_enforcement_dir: the cache directory, or nothing when no config dir can be named.
sm_enforcement_dir() {
  local base="${CLAUDE_CONFIG_DIR:-}"
  [[ -n "$base" ]] || { [[ -n "${HOME:-}" ]] && base="$HOME/.claude"; }
  [[ -n "$base" ]] && printf '%s\n' "$base/guard-state/enforcement"
}

# sm_clear_passes: remove every cached E20 pass, without python: the fallback
# when the recorder's own clear cannot run or does not finish in time.
sm_clear_passes() {
  local PATH="$SM_SYSTEM_PATH" dir
  dir=$(sm_enforcement_dir) || return 0
  # The status lines go too: one left saying "passed" would contradict an empty cache.
  [[ -n "$dir" ]] && rm -f -- "$dir/"*.json "$dir/"*.status 2>/dev/null
  return 0
}

# sm_drop_epoch: remove the current epoch, so no recorder holding it, from an
# earlier session, can match it any more. Used whenever no new epoch is written.
sm_drop_epoch() {
  local PATH="$SM_SYSTEM_PATH" dir
  dir=$(sm_enforcement_dir) || return 0
  [[ -n "$dir" ]] && rm -rf -- "$dir/.epoch" 2>/dev/null
  return 0
}

# sm_new_epoch: start a new epoch for the cache and print it. A recorder writes a
# pass only under the epoch the hook handed it, still current, so a recorder an
# earlier session left running (or still waiting on the lock) cannot write after
# this session's clear. The epoch is written to a private temporary file and
# renamed over .epoch, which replaces a planted link rather than following it.
# Fails (status 1) when it cannot; the caller then leaves no pass.
sm_new_epoch() {
  local PATH="$SM_SYSTEM_PATH" dir epoch tmp
  dir=$(sm_enforcement_dir) || return 1
  [[ -n "$dir" ]] || return 1
  ( umask 077 && mkdir -p -- "$dir" ) 2>/dev/null || return 1
  [[ -d "$dir" && ! -L "$dir" ]] || return 1
  epoch=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n') && [[ ${#epoch} -eq 32 ]] || return 1
  tmp=$( umask 077 && mktemp -- "$dir/.epoch.XXXXXX" ) 2>/dev/null || return 1
  # -T: a planted directory (or a link to one) at .epoch fails the move instead of receiving the file.
  if ! { printf '%s' "$epoch" > "$tmp" && mv -fT -- "$tmp" "$dir/.epoch"; } 2>/dev/null; then
    rm -f -- "$tmp" 2>/dev/null
    return 1
  fi
  printf '%s\n' "$epoch"
}

# sm_record_enforcement <project dir>: in a run session, start a new epoch, then
# remove every cached E20 pass synchronously (bounded well inside this hook's
# 10 s timeout, falling back to the bash clear), then start the recorder in the
# background with that epoch: the only writer of the pass the guards read (ADR
# 0063 §2-§3). It reads the network, which a SessionStart hook cannot wait for,
# so it is detached and bounded; until it writes a pass, guards refuse.
# Whatever fails here, no pass is left.
sm_record_enforcement() {
  local PATH="$SM_SYSTEM_PATH" here py setsid_bin timeout_bin epoch
  here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
  if ! { py=$(sm_trusted python3) && setsid_bin=$(sm_trusted setsid) && timeout_bin=$(sm_trusted timeout) \
         && [[ -r "$here/run_module.py" ]]; }; then
    sm_drop_epoch
    sm_clear_passes
    return 0
  fi
  # The epoch first: any recorder that checks it after this point sees it is not its own. With no new
  # epoch, the old one is dropped, so a recorder still holding it cannot match it either.
  epoch=$(sm_new_epoch) || { epoch=""; sm_drop_epoch; }
  ( cd / && "$timeout_bin" -k 1 4 "$py" -I -B -X pycache_prefix="$SM_NO_PYCACHE" "$here/run_module.py" "$here/.." \
      trimtab.adapters.enforcement_record clear "$1" </dev/null >/dev/null 2>&1 ) || sm_clear_passes
  [[ -n "$epoch" ]] || return 0  # no epoch, no recorder: it could not write a pass anyway
  # Off only, never on: with no recorder no pass is written, and guards refuse. Tests set it.
  [[ "${TRIMTAB_ENFORCEMENT_RECORDER:-1}" == 0 ]] && return 0
  ( cd / && "$setsid_bin" "$timeout_bin" -k 5 300 "$py" -I -B -X pycache_prefix="$SM_NO_PYCACHE" \
      "$here/run_module.py" "$here/.." trimtab.adapters.enforcement_record record "$1" "$epoch" \
      </dev/null >/dev/null 2>&1 & ) 2>/dev/null
  return 0
}

# ---- SessionStart hook ----------------------------------------------------
# Never blocks and always exits 0: SessionStart is not a gate. The guard
# enforces the mode on every write whatever this announcement said.
sm_main() {
  local cwd ctx msg
  sm_payload
  cwd="$sm_cwd"
  [[ -n "$cwd" && -d "$cwd" ]] || cwd="$PWD"
  sm_classify "$cwd" "$sm_sid"
  # The signals hold a branch name, which may carry a quote or a backslash.
  local s="$sm_signals"
  s=${s//\\/\\\\}; s=${s//\"/\\\"}
  if [[ "$sm_mode" == autonomous ]]; then
    sm_record_enforcement "$cwd"
    msg="Session mode: AUTONOMOUS PLAN RUN ($s)."
    ctx="$msg Work only as the rules for running an authorised plan allow: writes are limited to the authorised plan's .writes declaration plus its own progress records and issue-register entries, anything else is a register entry or a blocker, and the guard enforces this on every write. State this mode in your first response."
    printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$msg" "$ctx"
  else
    ctx="Session mode: INTERACTIVE ($s). The repository's write allowlist applies. If this session moves onto a plan/* branch it becomes an autonomous plan run for the rest of the session. State this mode in your first response."
    printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$ctx"
  fi
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  # Run as the hook (not sourced by guard-paths): every tool it runs is the system's.
  PATH="$SM_SYSTEM_PATH"
  export PATH
  sm_main
  exit 0
fi
