#!/usr/bin/env bash
# PreToolUse hook, matcher: mcp__.*
#
# MCP write policy: an MCP tool call that may write waits for the operator to
# approve it, one call at a time. Reads by name pass. The reasons, the
# measurements and the rejected options are in
# docs/adr/0013-guard-mcp-asks-before-mcp-tools-that-may-write.md.
#
# Answers, always with exit 0:
#   no output                      no decision; the session's permission rules apply
#   permissionDecision "ask"       the operator is prompted for this call
# Unlike the other guards this one never exits 2: the operator wants to approve
# a write, not lose the tool. Outside an interactive session an ask ends as a
# denial (measured, ADR 0013).
#
# Classifies by tool name only, never by the call's arguments:
#   a full name on the desktop app's exact-name list
#   (CCD_ALLOW below, ADR 0027)                     -> pass
#   a <tool> part on the verb-last read list, on any
#   server (TOOL_READ_ALLOW below, ADR 0013)        -> pass
#   <tool> starting gtm_                           -> ask: its verb is in an
#                                                      argument, not its name
#   mcp__<server>__<tool> whose <tool>, after at most one namespace from
#   ZohoBooks_, ZohoInventory_ or ZohoWorkdrive_ (exact case, stripped once),
#   starts with a read verb (get, list, search, query, read, in any case,
#   then _ or -)                                    -> pass, unless
#   <tool> contains key, token, secret, password or credential, in any
#   case                                            -> ask
#   anything else                                   -> ask
#
# Fails CLOSED by asking: an empty or unparseable payload, a missing parser
# (neither jq nor python3, ADR 0001), and a name that is not of the
# mcp__<server>__<tool> shape all ask.
#
# Session override: export HOOK_ALLOW_MCP=1
#   (must be exported before launching claude — an inline prefix on a Bash
#    command never reaches this process). The operator's decision only.

set -uo pipefail   # deliberately not -e: an unexpected non-zero must not
                   # short-circuit into a silent pass

[[ "${HOOK_ALLOW_MCP:-0}" == "1" ]] && exit 0

# The reason is printed inside a JSON string, so it carries fixed text and, at
# most, a name already checked against [A-Za-z0-9_-]. Never the arguments: they
# can hold anything, secrets included.
ask() { # ask <reason>
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"guard-mcp: %s"}}\n' "$1"
  exit 0
}

payload=$(cat)

# jq reports empty stdin as a successful parse of nothing; see ADR 0001.
[[ -z "$payload" ]] && ask "empty hook payload, so the call cannot be classified. Approve it only if you intended it."

# Emits tool_name when the payload is one JSON object whose tool_name is a
# string, and nothing otherwise. A parse error or a second document fails.
if command -v jq >/dev/null 2>&1; then
  name=$(printf '%s' "$payload" | jq -r 'if type == "object" then (.tool_name | strings) else empty end' 2>/dev/null)
  rc=$?
elif command -v python3 >/dev/null 2>&1; then
  name=$(printf '%s' "$payload" | python3 -c '
import json, sys
d = json.load(sys.stdin)
n = d.get("tool_name") if isinstance(d, dict) else None
sys.stdout.write(n if isinstance(n, str) else "")
' 2>/dev/null)
  rc=$?
else
  ask "neither jq nor python3 is available to read the call, so it cannot be classified (fail closed, ADR 0001). Approve it only if you intended it."
fi
[[ $rc -eq 0 ]] || ask "the hook payload could not be parsed, so the call cannot be classified. Approve it only if you intended it."

# The name must be one line of the characters Claude Code uses in MCP names.
# This check is also what makes $name safe to print inside the JSON reason.
[[ "$name" =~ ^mcp__[A-Za-z0-9_-]+$ ]] \
  || ask "the call is not named as an MCP tool (mcp__<server>__<tool>), so it cannot be classified. Approve it only if you intended it."

# The desktop app's own tools that only drive its UI or read its state pass, by
# exact full name (ADR 0027). Never a prefix or a glob: the same app servers
# also merge, escalate and destroy. A name not listed falls through to the
# classification below, so a new app tool asks unless its name is a read.
CCD_ALLOW=(
  mcp__ccd_turn__wrap_up mcp__ccd_turn__update_plan
  mcp__ccd_view__show_pane mcp__ccd_view__close_pane
  mcp__ccd_view__get_layout mcp__ccd_view__set_transcript_view
  mcp__ccd_window__get_window_layout mcp__ccd_window__open_session_in
  mcp__ccd_window__close_split mcp__ccd_window__set_sidebar_collapsed
  mcp__ccd_pr__get_status
  mcp__ccd_pr__unbind_pr mcp__ccd_pr__set_monitor
  mcp__ccd_session_mgmt__get_session mcp__ccd_session_mgmt__list_sessions
  mcp__ccd_session_mgmt__list_events mcp__ccd_session_mgmt__get_usage
  mcp__ccd_session_mgmt__search_session_transcripts
  mcp__ccd_host__get_storage_usage mcp__ccd_host__open_in_editor
  mcp__ccd_host__reveal_path mcp__ccd_host__request_keep_awake
  mcp__ccd_session__list_start_targets mcp__ccd_session__dismiss_task
  mcp__ccd_connectors__session_connectors_status
)
for allowed in "${CCD_ALLOW[@]}"; do
  # Quoted right-hand side: a literal string comparison, never a pattern.
  [[ "$name" == "$allowed" ]] && exit 0
done

rest="${name#mcp__}"
server="${rest%%__*}"
tool="${rest#*__}"
# No separator, an empty part, or a second separator: which part is the tool
# is ambiguous, so it is not classified as a read.
if [[ "$rest" != *__* || -z "$server" || -z "$tool" || "$tool" == *__* ]]; then
  ask "$name does not split into one server and one tool, so it cannot be classified. Approve it only if you intended it."
fi

# Read-only tools whose verb comes last pass by exact <tool> part, on any
# server, since a server's name is set per deployment (ADR 0013, amendment
# 2026-10-16). Never a suffix match: mark_all_notifications_read writes.
TOOL_READ_ALLOW=(
  pull_request_read issue_read actions_get actions_list
)
for allowed in "${TOOL_READ_ALLOW[@]}"; do
  # Quoted right-hand side: a literal string comparison, never a pattern.
  [[ "$tool" == "$allowed" ]] && exit 0
done

case "$tool" in
  gtm_*)
    ask "$name takes its verb in an argument, so every call to it asks (ADR 0013). Approve it only if you intended this call." ;;
esac

# One product namespace, from this exact list, is stripped once.
verb="$tool"
case "$verb" in
  ZohoBooks_*|ZohoInventory_*|ZohoWorkdrive_*) verb="${verb#*_}" ;;
esac

# Case-insensitive by bracket pattern, as guard-bash.sh does: nocasematch would
# leak to the rest of the script, and ${x,,} kills bash 3 with no output, which
# fails open.
case "$verb" in
  [Gg][Ee][Tt][_-]*|[Ll][Ii][Ss][Tt][_-]*|[Ss][Ee][Aa][Rr][Cc][Hh][_-]*|[Qq][Uu][Ee][Rr][Yy][_-]*|[Rr][Ee][Aa][Dd][_-]*)
    # A read whose tool part names secret material still asks. Matched
    # anywhere in <tool>, in any case; the server part is not checked.
    case "$tool" in
      *[Kk][Ee][Yy]*|*[Tt][Oo][Kk][Ee][Nn]*|*[Ss][Ee][Cc][Rr][Ee][Tt]*|*[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]*|*[Cc][Rr][Ee][Dd][Ee][Nn][Tt][Ii][Aa][Ll]*)
        ask "$name reads by name but may return secret material, so it asks (ADR 0013). Approve it only if you intended this call." ;;
    esac
    exit 0 ;;
esac

ask "$name may change something outside this session. Approve it only if you intended this call."
