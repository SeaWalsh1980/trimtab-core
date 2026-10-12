#!/usr/bin/env bash
# SessionStart hook: put the installed CLI first on PATH for the session's
# Bash calls, so commands, agents and routine prompts call it as `trimtab`.
#
# guard-paths refuses every Bash call whose text names the config directory's
# bin/, which is every way a session could otherwise reach the CLI. A bare word
# holds no / or ., so the guard's path scan never sees it, and the file that
# runs is still the protected one: no exemption in the guard, and no executable
# outside the protected set. The options rejected for this, and why, are in
# docs/adr/0018-sessions-reach-the-cli-on-path.md.
#
# The mechanism is CLAUDE_ENV_FILE: Claude Code runs that file's contents before
# each Bash command of the session. The line written names the directory
# absolutely, quoted with %q, and leaves $PATH to that later run.
#
# This hook cannot block anything and must not: SessionStart is not a gate, and
# a failure here leaves the guards exactly as they were. So it always exits 0,
# and when it cannot do its job it says so as a systemMessage, never silently:
# a session without `trimtab` would otherwise fail every method's first call
# with no hint why.
set -uo pipefail

cat >/dev/null 2>&1   # the payload carries nothing this hook needs

warn() {
  local why="$1"
  why=${why//\\/\\\\}; why=${why//\"/\\\"}
  printf '{"systemMessage":"trimtab is not on PATH: %s. The /trimtab-* commands, agents and routines cannot run it in this session.","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"trimtab is not on PATH (%s). Any step that runs trimtab will fail: stop and tell the operator, never call it by its path."}}\n' "$why" "$why"
  exit 0
}

if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
  cfg="${CLAUDE_CONFIG_DIR%/}"
elif [[ -n "${HOME:-}" ]]; then
  cfg="${HOME%/}/.claude"
else
  warn "neither CLAUDE_CONFIG_DIR nor HOME is set"
fi
[[ "$cfg" == /* ]] || warn "the config directory is not an absolute path"
bin="$cfg/bin"
[[ -x "$bin/trimtab" ]] || warn "the config directory's bin/trimtab is missing or not executable; re-run bootstrap"
[[ -n "${CLAUDE_ENV_FILE:-}" ]] || warn "CLAUDE_ENV_FILE is not set, so this Claude Code cannot carry PATH into Bash calls"

# shellcheck disable=SC2016  # $PATH stays literal: Claude Code expands it before each Bash call
line=$(printf 'export PATH=%q:"$PATH"' "$bin")
# Once per file: SessionStart fires again on resume, clear and compact.
if [[ -f "$CLAUDE_ENV_FILE" ]] && grep -qxF -- "$line" "$CLAUDE_ENV_FILE" 2>/dev/null; then
  exit 0
fi
printf '%s\n' "$line" >> "$CLAUDE_ENV_FILE" 2>/dev/null \
  || warn "could not append to CLAUDE_ENV_FILE"
exit 0
