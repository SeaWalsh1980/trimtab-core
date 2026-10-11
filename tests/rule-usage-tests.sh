#!/usr/bin/env bash
# Verification matrix for the rule-usage observer and its report.
# Usage: rule-usage-tests.sh [repo-root]      (defaults to this file's repo)
#
# Hermetic: every case runs against a temporary log, a temporary CLAUDE_CONFIG_DIR
# and a synthetic project tree, so nothing here reads the operator's real log,
# their real settings or their real sessions.
#
# The cases are grouped by the two failure shapes that matter:
#
#   the observer must never block   — an observability hook that exits non-zero
#                                     on a bad payload would put noise in front
#                                     of the operator on every rule load
#   the report must never be quietly empty
#                                   — zeros it cannot account for are exit 2,
#                                     with the reason on stderr
set -u

REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HOOK="$REPO/hooks/rule-usage.sh"
REPORT="$REPO/bin/rule-usage-report.py"
fails=0

TMP=$(mktemp -d -t rule-usage-tests.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

pass() { printf 'PASS  %-12s %s\n' "$1" "$2"; }
bad()  { printf 'FAIL  %-12s %s  -- %s\n' "$1" "$2" "$3"; fails=$((fails+1)); }

# --- fixtures -----------------------------------------------------------
PROJECT="$TMP/project"
mkdir -p "$PROJECT/.claude/rules" "$PROJECT/docs/reviews"
printf 'project root instructions\n' > "$PROJECT/CLAUDE.md"
printf -- '---\npaths: ["src/**"]\n---\nloaded rule\n' > "$PROJECT/.claude/rules/loaded.md"
printf -- '---\npaths: ["terraform/**"]\n---\nnever loaded rule\n' > "$PROJECT/.claude/rules/dormant.md"
# A project is marked by a harness file at its git root, so this one is a
# repository and carries a lock. BARE is a repository with no harness file.
mkdir -p "$PROJECT/.git"
printf '{}\n' > "$PROJECT/.claude/trimtab.json"
BARE="$TMP/bare"
mkdir -p "$BARE/.git" "$BARE/src"
CLAUDE_ONLY="$TMP/claude-only"
mkdir -p "$CLAUDE_ONLY/.git"
printf 'root instructions\n' > "$CLAUDE_ONLY/CLAUDE.md"

HOME_DIR="$TMP/claude-home"
mkdir -p "$HOME_DIR/projects"
cat > "$HOME_DIR/settings.json" <<'JSON'
{"hooks":{"InstructionsLoaded":[{"matcher":"*","hooks":[{"type":"command","command":"rule-usage.sh"}]}],
          "SessionStart":[{"matcher":"*","hooks":[{"type":"command","command":"rule-usage.sh"}]}],
          "SubagentStart":[{"matcher":"*","hooks":[{"type":"command","command":"rule-usage.sh"}]}],
          "PreToolUse":[{"matcher":"Skill","hooks":[{"type":"command","command":"rule-usage.sh"}]}],
          "UserPromptExpansion":[{"matcher":"*","hooks":[{"type":"command","command":"rule-usage.sh"}]}]}}
JSON

LOG="$TMP/rule-usage.jsonl"

fire() { # fire <payload> -> exit code in $rc, record appended to $LOG
  printf '%s' "$1" | CLAUDE_RULE_USAGE_LOG="$LOG" bash "$HOOK" >"$TMP/out" 2>"$TMP/err"
  rc=$?
}

has()    { grep -q -- "$1" "$LOG"; }
lastline() { tail -n 1 "$LOG"; }

# ======================================================================
echo "== observer =="

: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"transcript_path\":\"$TMP/t.jsonl\",\"cwd\":\"$PROJECT\",\"prompt_id\":\"p1\",\"file_path\":\"$PROJECT/.claude/rules/loaded.md\",\"memory_type\":\"Project\",\"load_reason\":\"path_glob_match\",\"globs\":[\"src/**\"],\"trigger_file_path\":\"$PROJECT/src/a.py\"}"
[[ $rc -eq 0 ]] && pass "observer" "a load exits 0" || bad "observer" "a load exits 0" "exit $rc"
has '"event":"InstructionsLoaded"' && pass "observer" "records the event" || bad "observer" "records the event" "$(lastline)"
has '"load_reason":"path_glob_match"' && pass "observer" "records why it loaded" || bad "observer" "records why it loaded" "$(lastline)"
has '"trigger_glob":"\*.py"' && pass "observer" "records what pulled it in, as an extension glob" || bad "observer" "records what pulled it in, as an extension glob" "$(lastline)"
has '"globs":\["src/\*\*"\]' && pass "observer" "records the rule's own glob beside it" || bad "observer" "records the rule's own glob beside it" "$(lastline)"
has 'trigger_file_path\|src/a.py' && bad "observer" "records no raw trigger path" "$(lastline)" || pass "observer" "records no raw trigger path"
has '"cwd"' && bad "observer" "records no raw cwd" "$(lastline)" || pass "observer" "records no raw cwd"
has "\"project_marker\":\"$PROJECT/.claude/trimtab.json\"" && pass "observer" "marks the project by its lock" || bad "observer" "marks the project by its lock" "$(lastline)"
has "$TMP/project\"" && bad "observer" "never records the project directory itself" "$(lastline)" || pass "observer" "never records the project directory itself"
has '"unknown_keys"' && bad "observer" "a load's trigger, parent and cwd are known keys" "$(lastline)" || pass "observer" "a load's trigger, parent and cwd are known keys"
size=$(wc -c < "$PROJECT/.claude/rules/loaded.md")
has "\"bytes\":$size" && pass "observer" "measures the file at load time" || bad "observer" "measures the file at load time" "want $size, got $(lastline)"
has '"transcript_path"' && bad "observer" "drops transcript_path" "it was recorded" || pass "observer" "drops transcript_path"

# --- work paths are reduced, never recorded (ADR 0010) -------------------
# A trigger with no extension, or an odd one, reduces to the bare glob: a
# suffix is free text and must not carry a name through.
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/CLAUDE.md\",\"memory_type\":\"Project\",\"load_reason\":\"path_glob_match\",\"trigger_file_path\":\"$PROJECT/src/Makefile\"}"
has '"trigger_glob":"\*"' && pass "observer" "a trigger with no extension reduces to *" || bad "observer" "a trigger with no extension reduces to *" "$(lastline)"
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/CLAUDE.md\",\"memory_type\":\"Project\",\"load_reason\":\"path_glob_match\",\"trigger_file_path\":\"$PROJECT/src/report.CLIENT-ACME-2026\"}"
has '"trigger_glob":"\*"' && pass "observer" "an odd suffix reduces to *, carrying no name" || bad "observer" "an odd suffix reduces to *, carrying no name" "$(lastline)"
has 'ACME' && bad "observer" "no part of the trigger's name survives" "$(lastline)" || pass "observer" "no part of the trigger's name survives"

# parent_file_path: kept only for an instruction or harness file, by shape.
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/docs/inc.md\",\"memory_type\":\"Project\",\"load_reason\":\"include\",\"parent_file_path\":\"$PROJECT/CLAUDE.md\"}"
has "\"parent_file_path\":\"$PROJECT/CLAUDE.md\"" && pass "observer" "an include from CLAUDE.md keeps its parent" || bad "observer" "an include from CLAUDE.md keeps its parent" "$(lastline)"
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/docs/inc.md\",\"memory_type\":\"Project\",\"load_reason\":\"include\",\"parent_file_path\":\"$PROJECT/.claude/rules/loaded.md\"}"
has '"parent_file_path"' && pass "observer" "an include from a file under .claude keeps its parent" || bad "observer" "an include from a file under .claude keeps its parent" "$(lastline)"
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/docs/inc.md\",\"memory_type\":\"Project\",\"load_reason\":\"include\",\"parent_file_path\":\"$PROJECT/src/notes.md\"}"
[[ $rc -eq 0 ]] && pass "observer" "an include from a work file exits 0" || bad "observer" "an include from a work file exits 0" "exit $rc"
has 'parent_file_path\|notes.md' && bad "observer" "an include from a work file drops its parent" "$(lastline)" || pass "observer" "an include from a work file drops its parent"
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/docs/inc.md\",\"memory_type\":\"Project\",\"load_reason\":\"include\",\"parent_file_path\":\"$PROJECT/CLAUDE.local.md\"}"
has '"parent_file_path"' && pass "observer" "the root CLAUDE.local.md counts as an instruction file" || bad "observer" "the root CLAUDE.local.md counts as an instruction file" "$(lastline)"
# A nested CLAUDE.md, or a .claude directory outside this project, would name
# a directory of the work, so both drop their parent.
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/docs/inc.md\",\"memory_type\":\"Project\",\"load_reason\":\"include\",\"parent_file_path\":\"$PROJECT/src/payments/CLAUDE.md\"}"
has 'parent_file_path\|payments' && bad "observer" "a nested CLAUDE.md drops its parent, naming no directory" "$(lastline)" || pass "observer" "a nested CLAUDE.md drops its parent, naming no directory"
: > "$LOG"
fire "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT\",\"file_path\":\"$PROJECT/docs/inc.md\",\"memory_type\":\"Project\",\"load_reason\":\"include\",\"parent_file_path\":\"$TMP/other/.claude/x.md\"}"
has 'parent_file_path\|other' && bad "observer" "a .claude directory outside the project drops its parent" "$(lastline)" || pass "observer" "a .claude directory outside the project drops its parent"
# The config directory counts, wherever the session's project is.
: > "$LOG"
printf '%s' "{\"hook_event_name\":\"InstructionsLoaded\",\"session_id\":\"s1\",\"cwd\":\"/x\",\"file_path\":\"$HOME_DIR/rules/y.md\",\"memory_type\":\"User\",\"load_reason\":\"include\",\"parent_file_path\":\"$HOME_DIR/CLAUDE.md\"}" \
  | CLAUDE_CONFIG_DIR="$HOME_DIR" CLAUDE_RULE_USAGE_LOG="$LOG" bash "$HOOK" >/dev/null 2>&1
has "\"parent_file_path\":\"$HOME_DIR/CLAUDE.md\"" && pass "observer" "a parent under the config directory is kept" || bad "observer" "a parent under the config directory is kept" "$(lastline)"

# project_marker: the lock, else CLAUDE.md, else the .claude directory, else
# nothing — and it is the same wherever in the project the session started.
: > "$LOG"
fire "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"s1\",\"cwd\":\"$PROJECT/src/deep\",\"source\":\"startup\"}"
has "\"project_marker\":\"$PROJECT/.claude/trimtab.json\"" && pass "observer" "a session in a subdirectory resolves to the project's lock" || bad "observer" "a session in a subdirectory resolves to the project's lock" "$(lastline)"
: > "$LOG"
fire "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"s1\",\"cwd\":\"$CLAUDE_ONLY\",\"source\":\"startup\"}"
has "\"project_marker\":\"$CLAUDE_ONLY/CLAUDE.md\"" && pass "observer" "without a lock, CLAUDE.md marks the project" || bad "observer" "without a lock, CLAUDE.md marks the project" "$(lastline)"
mkdir -p "$BARE/.claude"
: > "$LOG"
fire "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"s1\",\"cwd\":\"$BARE\",\"source\":\"startup\"}"
has "\"project_marker\":\"$BARE/.claude\"" && pass "observer" "with neither, the .claude directory marks the project" || bad "observer" "with neither, the .claude directory marks the project" "$(lastline)"
rmdir "$BARE/.claude"
: > "$LOG"
fire "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"s1\",\"cwd\":\"$BARE/src\",\"source\":\"startup\"}"
[[ $rc -eq 0 ]] && pass "observer" "a project with no harness file exits 0" || bad "observer" "a project with no harness file exits 0" "exit $rc"
has 'project_marker' && bad "observer" "a project with no harness file records no marker" "$(lastline)" || pass "observer" "a project with no harness file records no marker"
has '"cwd"\|bare' && bad "observer" "and still no raw cwd" "$(lastline)" || pass "observer" "and still no raw cwd"
: > "$LOG"
fire '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"relative/dir","source":"startup"}'
[[ $rc -eq 0 ]] && has 'project_marker' && bad "observer" "a relative cwd records no marker" "$(lastline)" || pass "observer" "a relative cwd records no marker"

: > "$LOG"
fire '{"hook_event_name":"InstructionsLoaded","session_id":"s1","cwd":"/x","memory_type":"User","load_reason":"session_start"}'
has '"missing_keys":\["file_path"\]' && pass "observer" "names a required field that did not arrive" || bad "observer" "names a required field that did not arrive" "$(lastline)"

: > "$LOG"
fire '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"/x","source":"startup","model":"m","some_new_field":1}'
has '"unknown_keys":\["some_new_field"\]' && pass "observer" "names a field the schema does not know" || bad "observer" "names a field the schema does not know" "$(lastline)"

# The whole base payload as the CLI sends it today (2.1.280), including the
# keys that arrived after this observer was written. Any of them reading as
# drift means every weekly report exits 1 naming them, which is how a "needs a
# person" section stops being read.
: > "$LOG"
fire '{"hook_event_name":"SessionStart","session_id":"s1","transcript_path":"/t","cwd":"/x","scratchpad_dir":"/tmp/s","prompt_id":"p","permission_mode":"default","agent_id":"a","agent_type":"general-purpose","served_call":"c","caller_session_id":"cs","effort":{"level":"high"},"source":"startup"}'
has '"unknown_keys"' && bad "observer" "today's base payload is fully known" "$(lastline)" || pass "observer" "today's base payload is fully known"
has '"agent_type":"general-purpose"' && pass "observer" "records agent_type, so a load is attributable to its subagent" || bad "observer" "records agent_type, so a load is attributable to its subagent" "$(lastline)"
has 'scratchpad_dir' && bad "observer" "knows scratchpad_dir without recording it" "it was recorded" || pass "observer" "knows scratchpad_dir without recording it"
has '"effort"' && bad "observer" "knows effort without recording it" "it was recorded" || pass "observer" "knows effort without recording it"

: > "$LOG"
fire '{"hook_event_name":"PreToolUse","session_id":"s1","cwd":"/x","tool_name":"Skill","tool_use_id":"t","mcp_server":{"name":"x"},"tool_input":{"skill":"a"}}'
has '"unknown_keys"' && bad "observer" "a PreToolUse mcp_server is known" "$(lastline)" || pass "observer" "a PreToolUse mcp_server is known"

: > "$LOG"
fire '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"/x","source":"startup","session_title":"SECRET-TITLE-XYZ"}'
has 'SECRET-TITLE-XYZ' && bad "observer" "never records a session title" "it was recorded" || pass "observer" "never records a session title"

: > "$LOG"
fire '{"hook_event_name":"UserPromptSubmit","session_id":"s1","cwd":"/x","prompt":"SECRET-PROMPT-XYZ"}'
[[ $rc -eq 0 ]] && pass "observer" "an unregistered event still exits 0" || bad "observer" "an unregistered event still exits 0" "exit $rc"
has '"reason":"unregistered-event"' && pass "observer" "says the event was not one of its three" || bad "observer" "says the event was not one of its three" "$(lastline)"
has 'SECRET-PROMPT-XYZ' && bad "observer" "never records prompt text" "it was recorded" || pass "observer" "never records prompt text"

: > "$LOG"
fire 'NOT JSON AT ALL'
[[ $rc -eq 0 ]] && pass "observer" "garbage exits 0 (never blocks)" || bad "observer" "garbage exits 0 (never blocks)" "exit $rc"
has '"reason":"unparseable-payload"' && pass "observer" "records its own parse failure" || bad "observer" "records its own parse failure" "$(lastline)"

: > "$LOG"
fire '{"hook_event_name":"SubagentStart","session_id":"s1","cwd":"/x","agent_id":"a1","agent_type":"xx-planner"}'
has '"agent_type":"xx-planner"' && pass "observer" "records the agent type" || bad "observer" "records the agent type" "$(lastline)"

: > "$LOG"
fire '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"/x","source":"startup"}'
fire '{"hook_event_name":"SessionStart","session_id":"s2","cwd":"/x","source":"resume"}'
[[ $(wc -l < "$LOG") -eq 2 ]] && pass "observer" "appends rather than replacing" || bad "observer" "appends rather than replacing" "$(wc -l < "$LOG") line(s)"

printf '%s' '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"/x","source":"startup"}' \
  | CLAUDE_RULE_USAGE_LOG="/proc/cannot/write.jsonl" bash "$HOOK" >/dev/null 2>"$TMP/err"
rc=$?
[[ $rc -eq 1 ]] && pass "observer" "an unwritable log is loud, not silent" || bad "observer" "an unwritable log is loud, not silent" "exit $rc"
grep -q 'cannot write' "$TMP/err" && pass "observer" "says where it could not write" || bad "observer" "says where it could not write" "$(cat "$TMP/err")"

# --- skills and slash commands ------------------------------------------
: > "$LOG"
fire '{"hook_event_name":"PreToolUse","session_id":"s1","cwd":"/x","tool_name":"Skill","tool_use_id":"t1","tool_input":{"skill":"superpowers:brainstorming","args":"SECRET-ARGS-XYZ"}}'
has '"skill":"superpowers:brainstorming"' && pass "observer" "records the skill invoked" || bad "observer" "records the skill invoked" "$(lastline)"
has 'SECRET-ARGS-XYZ' && bad "observer" "never records a skill's arguments" "they were recorded" || pass "observer" "never records a skill's arguments"

: > "$LOG"
fire '{"hook_event_name":"UserPromptExpansion","session_id":"s1","cwd":"/x","expansion_type":"slash_command","command_name":"xx-review","command_args":"SECRET-ARGS-XYZ","command_source":"project","prompt":"SECRET-PROMPT-XYZ"}'
has '"command_name":"xx-review"' && pass "observer" "records the slash command" || bad "observer" "records the slash command" "$(lastline)"
has '"command_source":"project"' && pass "observer" "records where the command came from" || bad "observer" "records where the command came from" "$(lastline)"
has 'SECRET' && bad "observer" "never records a command's args or prompt" "they were recorded" || pass "observer" "never records a command's args or prompt"

: > "$LOG"
fire '{"hook_event_name":"PreToolUse","session_id":"s1","cwd":"/x","tool_name":"Bash","tool_use_id":"t2","tool_input":{"command":"ls"}}'
has '"tool_name":"Bash"' && pass "observer" "names a tool the matcher should not have caught" || bad "observer" "names a tool the matcher should not have caught" "$(lastline)"
has '"skill"' && bad "observer" "no skill on a non-Skill tool" "one was recorded" || pass "observer" "no skill on a non-Skill tool"
has 'ls' && bad "observer" "never records another tool's input" "it was recorded" || pass "observer" "never records another tool's input"

# PreToolUse and UserPromptExpansion treat exit 2 as "refuse this". An
# observer that ever returned 2 would silently eat the operator's skills and
# slash commands, so no payload may produce it.
blocked=0
for payload in \
  '{"hook_event_name":"PreToolUse","session_id":"s1","cwd":"/x","tool_name":"Skill","tool_input":{"skill":"x"}}' \
  '{"hook_event_name":"PreToolUse"}' \
  '{"hook_event_name":"UserPromptExpansion","command_name":"x","expansion_type":"slash_command"}' \
  '{"hook_event_name":"UserPromptExpansion"}' \
  '{"hook_event_name":"NotAnEventWeKnow"}' \
  '[]' \
  'NOT JSON AT ALL' \
  ''
do
  fire "$payload"
  [[ $rc -eq 2 ]] && blocked=$((blocked+1))
done
[[ $blocked -eq 0 ]] && pass "observer" "never exits 2, so it can never block a skill or a command" \
                     || bad "observer" "never exits 2, so it can never block a skill or a command" "$blocked payload(s) returned 2"

# ======================================================================
echo "== report =="

report() { # report <args...> -> exit code in $rc, stdout in $TMP/report, stderr in $TMP/err
  CLAUDE_CONFIG_DIR="$HOME_DIR" python3 "$REPORT" "$@" >"$TMP/report" 2>"$TMP/err"
  rc=$?
}

report --log "$TMP/never-written.jsonl"
[[ $rc -eq 2 ]] && pass "report" "no log at all is exit 2" || bad "report" "no log at all is exit 2" "exit $rc"
grep -q 'has never written a record' "$TMP/err" && pass "report" "says the observer never ran" || bad "report" "says the observer never ran" "$(cat "$TMP/err")"

: > "$TMP/empty.jsonl"
touch "$HOME_DIR/projects/sess.jsonl"
mkdir -p "$HOME_DIR/projects/a-project" && touch "$HOME_DIR/projects/a-project/sess.jsonl"
report --log "$TMP/empty.jsonl"
[[ $rc -eq 2 ]] && pass "report" "sessions ran but nothing was recorded is exit 2" || bad "report" "sessions ran but nothing was recorded is exit 2" "exit $rc"
grep -q 'not firing' "$TMP/err" && pass "report" "names the hook as the fault" || bad "report" "names the hook as the fault" "$(cat "$TMP/err")"

# A window with real activity in it.
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
MARKER="$PROJECT/.claude/trimtab.json"
FULL="$TMP/full.jsonl"
{
  printf '{"v":1,"ts":"%s","event":"SessionStart","session_id":"s1","project_marker":"%s","source":"startup","model":"m"}\n' "$NOW" "$MARKER"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","project_marker":"%s","file_path":"%s","memory_type":"Project","load_reason":"session_start","bytes":26}\n' "$NOW" "$MARKER" "$PROJECT/CLAUDE.md"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","project_marker":"%s","file_path":"%s","memory_type":"Project","load_reason":"path_glob_match","trigger_glob":"%s","bytes":40}\n' "$NOW" "$MARKER" "$PROJECT/.claude/rules/loaded.md" "*.py"
  printf '{"v":1,"ts":"%s","event":"SubagentStart","session_id":"s1","project_marker":"%s","agent_type":"xx-planner"}\n' "$NOW" "$MARKER"
  printf '{"v":1,"ts":"%s","event":"PreToolUse","session_id":"s1","project_marker":"%s","tool_name":"Skill","skill":"used-skill"}\n' "$NOW" "$MARKER"
  printf '{"v":1,"ts":"%s","event":"UserPromptExpansion","session_id":"s1","project_marker":"%s","expansion_type":"slash_command","command_name":"xx-review","command_source":"project"}\n' "$NOW" "$MARKER"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","project_marker":"%s","file_path":"%s","memory_type":"User","load_reason":"session_start","bytes":30}\n' "$NOW" "$MARKER" "$HOME_DIR/rules/loaded-doctrine.md"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","project_marker":"%s","file_path":"%s","memory_type":"User","load_reason":"session_start","bytes":30}\n' "$NOW" "$MARKER" "$TMP/elsewhere/linked-doctrine.md"
} > "$FULL"

# The same window as the observer wrote it before it stopped recording work
# paths: raw cwd and trigger_file_path.
LEGACY="$TMP/legacy.jsonl"
{
  printf '{"v":1,"ts":"%s","event":"SessionStart","session_id":"s1","cwd":"%s","source":"startup","model":"m"}\n' "$NOW" "$PROJECT"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","cwd":"%s","file_path":"%s","memory_type":"Project","load_reason":"session_start","bytes":26}\n' "$NOW" "$PROJECT" "$PROJECT/CLAUDE.md"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","cwd":"%s","file_path":"%s","memory_type":"Project","load_reason":"path_glob_match","trigger_file_path":"%s","bytes":40}\n' "$NOW" "$PROJECT" "$PROJECT/.claude/rules/loaded.md" "$PROJECT/src/a.py"
  printf '{"v":1,"ts":"%s","event":"SubagentStart","session_id":"s1","cwd":"%s","agent_type":"xx-planner"}\n' "$NOW" "$PROJECT"
  printf '{"v":1,"ts":"%s","event":"PreToolUse","session_id":"s1","cwd":"%s","tool_name":"Skill","skill":"used-skill"}\n' "$NOW" "$PROJECT"
  printf '{"v":1,"ts":"%s","event":"UserPromptExpansion","session_id":"s1","cwd":"%s","expansion_type":"slash_command","command_name":"xx-review","command_source":"project"}\n' "$NOW" "$PROJECT"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","cwd":"%s","file_path":"%s","memory_type":"User","load_reason":"session_start","bytes":30}\n' "$NOW" "$PROJECT" "$HOME_DIR/rules/loaded-doctrine.md"
  printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","cwd":"%s","file_path":"%s","memory_type":"User","load_reason":"session_start","bytes":30}\n' "$NOW" "$PROJECT" "$TMP/elsewhere/linked-doctrine.md"
} > "$LEGACY"

# The doctrine corpus: loaded in every project, owned by none of them. One
# file is reached through a symlink, because that is how the real one is laid
# out — ~/.claude/rules points into the config repository, and matching on the
# literal path would call a loaded file dormant.
mkdir -p "$HOME_DIR/rules/nested" "$TMP/elsewhere"
printf -- '---\nname: loaded-doctrine\n---\nx\n' > "$HOME_DIR/rules/loaded-doctrine.md"
printf -- '---\nname: dormant-doctrine\n---\nx\n' > "$HOME_DIR/rules/dormant-doctrine.md"
printf -- '---\nname: nested-doctrine\n---\nx\n' > "$HOME_DIR/rules/nested/nested-doctrine.md"
printf -- '---\nname: linked-doctrine\n---\nx\n' > "$TMP/elsewhere/linked-doctrine.md"
ln -s "$TMP/elsewhere/linked-doctrine.md" "$HOME_DIR/rules/linked-doctrine.md"

mkdir -p "$HOME_DIR/skills/used-skill" "$HOME_DIR/skills/dormant-skill"
printf -- '---\nname: used-skill\n---\n' > "$HOME_DIR/skills/used-skill/SKILL.md"
printf -- '---\nname: dormant-skill\n---\n' > "$HOME_DIR/skills/dormant-skill/SKILL.md"
mkdir -p "$PROJECT/.claude/commands"
printf 'a command\n' > "$PROJECT/.claude/commands/xx-review.md"
printf 'another\n' > "$PROJECT/.claude/commands/never-typed.md"

report --log "$FULL"
[[ $rc -eq 0 ]] && pass "report" "a clean window is exit 0" || bad "report" "a clean window is exit 0" "exit $rc: $(cat "$TMP/err")"
grep -q 'dormant.md' "$TMP/report" && pass "report" "names the rule that never loaded" || bad "report" "names the rule that never loaded" "absent"
grep -q 'loaded.md' "$TMP/report" && pass "report" "names the rule that did load" || bad "report" "names the rule that did load" "absent"
grep -q 'xx-planner' "$TMP/report" && pass "report" "counts the subagent" || bad "report" "counts the subagent" "absent"
grep -q 'used-skill' "$TMP/report" && pass "report" "counts the skill that was invoked" || bad "report" "counts the skill that was invoked" "absent"
grep -q 'xx-review' "$TMP/report" && pass "report" "counts the slash command" || bad "report" "counts the slash command" "absent"
grep -q 'dormant-skill' "$TMP/report" && pass "report" "names the installed skill nobody used" || bad "report" "names the installed skill nobody used" "absent"
grep -q 'never-typed' "$TMP/report" && pass "report" "names the command nobody typed" || bad "report" "names the command nobody typed" "absent"
grep -q 'Doctrine — loaded in every project' "$TMP/report" && pass "report" "gives the doctrine its own section" || bad "report" "gives the doctrine its own section" "absent"
grep -q 'loaded-doctrine' "$TMP/report" && pass "report" "counts a doctrine file that loaded" || bad "report" "counts a doctrine file that loaded" "absent"
grep -q 'dormant-doctrine' "$TMP/report" && pass "report" "names the doctrine file nobody loaded" || bad "report" "names the doctrine file nobody loaded" "absent"
grep -q 'nested/nested-doctrine.md' "$TMP/report" && pass "report" "finds a rule in a subdirectory" || bad "report" "finds a rule in a subdirectory" "absent"
sed -n '/Did not load at all/,/^$/p' "$TMP/report" | grep -q 'linked-doctrine' \
  && bad "report" "matches a rule loaded through its real path" "reported as never loaded" \
  || pass "report" "matches a rule loaded through its real path"
grep -q 'accounted for above' "$TMP/report" && pass "report" "reconciles every load against a section" || bad "report" "reconciles every load against a section" "absent"
grep -q 'Plugin skills are not in those counts' "$TMP/report" && pass "report" "says which skills it did not count" || bad "report" "says which skills it did not count" "absent"
grep -q "$PROJECT" "$TMP/report" && pass "report" "found the project from its marker" || bad "report" "found the project from its marker" "absent"
grep -q 'top trigger globs' "$TMP/report" && grep -q '`\*.py` ×1' "$TMP/report" && pass "report" "tables what pulled a rule in, as a glob" || bad "report" "tables what pulled a rule in, as a glob" "absent"
grep -q 'Legacy records' "$TMP/report" && bad "report" "a new-format log is not called legacy" "named" || pass "report" "a new-format log is not called legacy"
grep -q 'src/a.py' "$TMP/report" && bad "report" "shows no trigger path" "shown" || pass "report" "shows no trigger path"

# A log written before the change: raw cwd and trigger_file_path. It must read
# without a crash, build the same corpus table, name itself legacy, and never
# print the raw trigger path.
cp "$TMP/report" "$TMP/report-new"
report --log "$LEGACY"
[[ $rc -eq 0 ]] && pass "report" "a legacy log is still exit 0" || bad "report" "a legacy log is still exit 0" "exit $rc: $(cat "$TMP/err")"
grep -q 'Legacy records' "$TMP/report" && pass "report" "says the records are legacy" || bad "report" "says the records are legacy" "absent"
grep -q 'src/a.py' "$TMP/report" && bad "report" "a legacy trigger path is not shown" "shown" || pass "report" "a legacy trigger path is not shown"
grep -q "$PROJECT" "$TMP/report" && pass "report" "a legacy cwd still finds the project" || bad "report" "a legacy cwd still finds the project" "absent"
for f in report-new report; do
  sed -n '/^## '"${PROJECT//\//\\/}"'/,/^## Context cost/p' "$TMP/$f" | grep '^| ' | grep -v 'trigger\|×' > "$TMP/table-$f"
done
[[ -s "$TMP/table-report" ]] && diff -q "$TMP/table-report-new" "$TMP/table-report" >/dev/null \
  && pass "report" "a marker and a cwd build the same corpus table" \
  || bad "report" "a marker and a cwd build the same corpus table" "$(diff "$TMP/table-report-new" "$TMP/table-report" | head -3)"

# A session whose project has no harness file leaves no marker: it adds no
# project, the report says so, and --project still names one.
NOMARK="$TMP/nomark.jsonl"
printf '{"v":1,"ts":"%s","event":"SessionStart","session_id":"s9","source":"startup"}\n' "$NOW" > "$NOMARK"
report --log "$NOMARK"
grep -q 'Sessions with no project marker: 1' "$TMP/report" && pass "report" "says a session had no project marker" || bad "report" "says a session had no project marker" "absent"
grep -q 'could be resolved from the observed project markers' "$TMP/report" && pass "report" "says no corpus table could be built" || bad "report" "says no corpus table could be built" "absent"
report --log "$NOMARK" --project "$PROJECT"
grep -q "^## $PROJECT" "$TMP/report" && pass "report" "--project still names a project" || bad "report" "--project still names a project" "absent"

printf 'not json\n' >> "$FULL"
report --log "$FULL"
[[ $rc -eq 1 ]] && pass "report" "an unreadable line is exit 1" || bad "report" "an unreadable line is exit 1" "exit $rc"
grep -q 'did not parse' "$TMP/report" && pass "report" "counts the unreadable line in the report" || bad "report" "counts the unreadable line in the report" "absent"

report --log "$FULL" --out "$TMP/out.md"
[[ -s "$TMP/out.md" ]] && pass "report" "--out writes the report" || bad "report" "--out writes the report" "empty or absent"

printf '# review\n\nPLAN-CITATIONS: plan=present files=2 rule_files=1 clauses=9 cited=3 uncited=6 unmatched=0 ambiguous=0 unparsed=0 out_of_scope=0\n' \
  > "$PROJECT/docs/reviews/$(date -u +%Y-%m-%d)-track.md"
report --log "$FULL"
grep -q 'present 1' "$TMP/report" && pass "report" "tallies a PLAN-CITATIONS line" || bad "report" "tallies a PLAN-CITATIONS line" "absent"
grep -q 'uncited 6' "$TMP/report" && pass "report" "sums the uncited clauses" || bad "report" "sums the uncited clauses" "absent"

printf '{"v":1,"ts":"%s","event":"InstructionsLoaded","session_id":"s1","project_marker":"%s","file_path":"/nowhere/ORPHAN.md","memory_type":"Managed","load_reason":"session_start","bytes":4096}\n' "$NOW" "$MARKER" >> "$FULL"
report --log "$FULL"
grep -q 'no section above accounts for' "$TMP/report" && pass "report" "names a load it cannot place" || bad "report" "names a load it cannot place" "absent"
grep -q 'ORPHAN.md' "$TMP/report" && pass "report" "names which file that was" || bad "report" "names which file that was" "absent"

report --days 0 --log "$FULL"
[[ $rc -eq 2 ]] && pass "report" "a nonsense window is refused" || bad "report" "a nonsense window is refused" "exit $rc"

echo
if [[ $fails -eq 0 ]]; then echo "ALL PASS"; exit 0; else echo "$fails FAILURE(S)"; exit 1; fi
