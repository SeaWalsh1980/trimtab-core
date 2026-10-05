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
#   <tool> starting gtm_                            -> ask: its verb is in an
#                                                      argument, not its name
#   mcp__<server>__<tool> whose <tool>, after at most one namespace from
#   ZohoBooks_, ZohoInventory_ or ZohoWorkdrive_ (exact case, stripped once),
#   starts with a read verb (get, list, search, query, read, in any case,
#   then _ or -)                                    -> pass
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

rest="${name#mcp__}"
server="${rest%%__*}"
tool="${rest#*__}"
# No separator, an empty part, or a second separator: which part is the tool
# is ambiguous, so it is not classified as a read.
if [[ "$rest" != *__* || -z "$server" || -z "$tool" || "$tool" == *__* ]]; then
  ask "$name does not split into one server and one tool, so it cannot be classified. Approve it only if you intended it."
fi

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
    exit 0 ;;
esac

ask "$name may change something outside this session. Approve it only if you intended this call."
