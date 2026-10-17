#!/usr/bin/env bash
# Tests the installer against a fixture instance: what it writes into the
# generated settings, what it refuses before any link switches, and what
# retention may delete. Each check builds its own throwaway home, so nothing
# touches the operator's real install.
set -euo pipefail
# Tests the guards as they ship, whatever the caller exported: bootstrap's
# self-probe expects guard-security to block, and an inherited HOOK_ALLOW_*
# makes it return 0. Clear them all before bootstrap runs.
for v in $(compgen -v HOOK_ALLOW_); do unset "$v"; done
# The same for TRIMTAB_INSTANCE: a Claude session exports its own, and a probe
# that leaned on it passed here while failing in CI, where nothing sets it.
unset TRIMTAB_INSTANCE
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work=$(mktemp -d -t bootstrap-env.XXXXXX)
trap 'rm -rf "$work"' EXIT

fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1${2:+ ($2)}"; fails=$((fails + 1)); }

# A throwaway home with a fixture instance beside it. Prints the home path.
new_home() {
  local h
  h=$(mktemp -d -p "$work" home.XXXXXX)
  python3 "$repo/tests/trimtab/instance_fixture.py" "$h" >/dev/null
  echo "$h"
}
inst_of() { echo "$1/instance"; }

# boot <home> [runner] [args...]: run an installer sandboxed to <home>.
# <runner> is the installer script to run (default: this checkout's).
# Sets $out (stdout and stderr together) and $rc.
boot() {
  local h="$1" runner="$2"; shift 2
  set +e
  out=$(CLAUDE_CONFIG_DIR="$h/.claude" XDG_DATA_HOME="$h/share" "$runner" --no-timer --allow-worktree "$@" 2>&1)
  rc=$?
  set -e
}
live="$repo/bootstrap.sh"

# settings_get <home> <dotted.key>: the generated settings' value, or <absent>.
settings_get() {
  python3 -c '
import json, sys
v = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    if not isinstance(v, dict) or k not in v:
        v = "<absent>"; break
    v = v[k]
print(v)' "$1/.claude/settings.json" "$2"
}
no_links() { [[ ! -e "$1/.claude/hooks" && ! -L "$1/.claude/hooks" ]]; }

check_writes_instance_values() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 \
     && "$(settings_get "$h" env.TRIMTAB_INSTANCE)" == "$inst" \
     && "$(settings_get "$h" env.TRIMTAB_SECRET_PATTERNS)" == "$inst/guards/secrets.patterns" \
     && "$(settings_get "$h" env.TRIMTAB_SECRET_PACKS)" == '<absent>' \
     && "$(settings_get "$h" theme)" == dark \
     && "$(readlink -f "$h/.claude/rules")" == "$(readlink -f "$inst/rules")" \
     && "$(readlink -f "$h/.claude/hooks")" == "$(readlink -f "$repo/hooks")" \
     && "$(readlink -f "$h/.claude/CLAUDE.md")" == "$(readlink -f "$inst/CLAUDE.md")" ]]; then
    pass "writes the instance's values and links the two roots"
  else
    fail "writes the instance's values and links the two roots" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_base_hooks_survive() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 ]] && python3 - "$h/.claude/settings.json" "$repo/settings.base.json" <<'PY'
import json, sys
merged, base = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
for event, groups in base["hooks"].items():
    for group in groups:
        assert group in merged["hooks"][event], (event, group)
PY
  then
    pass "every base hook survives the merge"
  else
    fail "every base hook survives the merge" "rc=$rc"
  fi
}

# A run's commits and PR bodies carry its session link and default attribution
# unless the installed settings turn them off
# (docs/adr/0022-no-reference-to-the-run-in-what-it-writes.md,
# docs/adr/0025-installed-settings-turn-commit-and-pr-attribution-off.md). The
# empty strings must survive the merge and the pruning of empty containers, so
# the value is read back from the generated file and compared as JSON, exactly.
check_installed_settings_turn_commit_and_pr_attribution_off() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  local found=""
  [[ $rc -eq 0 ]] && found=$(python3 - "$h/.claude/settings.json" <<'PY'
import json, sys
print(json.dumps(json.load(open(sys.argv[1])).get("attribution"), sort_keys=True))
PY
)
  if [[ $rc -eq 0 && "$found" == '{"commit": "", "pr": "", "sessionUrl": false}' ]]; then
    pass "the installed settings turn commit and PR attribution and the session link off"
  else
    fail "the installed settings turn commit and PR attribution and the session link off" "rc=$rc attribution=${found:-<unread>}"
  fi
}

check_refuses_disable_all_hooks() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  echo '{"disableAllHooks": true}' > "$inst/machine.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *disableAllHooks* && "$out" == *machine.json* ]] && no_links "$h"; then
    pass "refuses disableAllHooks before any link switches"
  else
    fail "refuses disableAllHooks before any link switches" "rc=$rc"; echo "$out" | tail -5
  fi
}

# The base owns attribution (docs/adr/0026-bootstrap-refuses-an-attribution-override.md).
# A later layer's value would replace or reshape it with no warning, and the
# session link would come back in commits and PR bodies. Any value is refused,
# a scalar or an object, and nothing is written.
check_refuses_a_layer_that_sets_attribution() {
  local h inst layer value
  for layer in settings.instance.json machine.json; do
    # The last two are values the base itself has set, the current one last: a
    # layer repeating the base's exact value is refused too (ADR 0026).
    for value in '{"commit": "x"}' '{"sessionUrl": true}' 'false' 'null' '{"sessionUrl": false}' \
                 '{"commit": "", "pr": "", "sessionUrl": false}'; do
      h=$(new_home); inst=$(inst_of "$h")
      echo "{\"attribution\": $value}" > "$inst/$layer"
      boot "$h" "$live" --instance "$inst"
      # The refusal line itself names the layer: bootstrap prints machine.json's
      # name on every run, so a bare match would pass whichever layer was refused.
      if [[ $rc -eq 1 && "$out" == *"REFUSED: $layer sets \`attribution\`"* \
            && ! -e "$h/.claude/settings.json" ]] && no_links "$h"; then
        pass "refuses attribution $value in $layer before anything is written"
      else
        fail "refuses attribution $value in $layer before anything is written" "rc=$rc"; echo "$out" | tail -5
      fi
    done
  done
}

# Over a live install the refusal leaves it as it was: the settings file is
# byte-for-byte the same and the links point where they did. A fresh home
# cannot show this, since it has no settings.json to overwrite.
check_attribution_refusal_leaves_a_live_install_alone() {
  local h inst before links_before
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  [[ $rc -eq 0 ]] || { fail "an attribution refusal leaves a live install alone" "first install rc=$rc"; return; }
  before=$(sha256sum < "$h/.claude/settings.json")
  links_before=$(readlink "$h/.claude/hooks")
  echo '{"attribution": {"commit": "x"}}' > "$inst/machine.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *"REFUSED: machine.json sets \`attribution\`"* \
        && "$(sha256sum < "$h/.claude/settings.json")" == "$before" \
        && "$(readlink "$h/.claude/hooks")" == "$links_before" ]]; then
    pass "an attribution refusal leaves a live install's settings and links as they were"
  else
    fail "an attribution refusal leaves a live install's settings and links as they were" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_refuses_hooks_replaced() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  echo '{"hooks": "off"}' > "$inst/settings.instance.json"
  boot "$h" "$live" --instance "$inst"
  # machine.json is created before the merge (it is one of the layers), so the
  # refusal must not claim that nothing was changed.
  if [[ $rc -eq 1 && "$out" == *settings.instance.json* && "${out,,}" != *"nothing was changed"* ]] \
     && no_links "$h"; then
    pass "refuses a layer that replaces hooks, naming the layer, without claiming nothing changed"
  else
    fail "refuses a layer that replaces hooks, naming the layer, without claiming nothing changed" "rc=$rc"; echo "$out" | tail -5
  fi
}

# A guard override in a settings layer's env would be written into the generated
# settings and lift that guard for every session, while every hook registration
# still looks intact. It is refused by key, whatever its value, and the value is
# never printed.
check_refuses_hook_allow_in_machine_env() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  echo '{"env": {"HOOK_ALLOW_SECURITY": "value-must-not-be-printed"}}' > "$inst/machine.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *HOOK_ALLOW_SECURITY* && "$out" == *machine.json* \
     && "$out" != *value-must-not-be-printed* && ! -e "$h/.claude/settings.json" ]] && no_links "$h"; then
    pass "refuses a HOOK_ALLOW_* key in machine.json's env, naming the key and not its value"
  else
    fail "refuses a HOOK_ALLOW_* key in machine.json's env, naming the key and not its value" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_refuses_hook_allow_in_instance_env() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  echo '{"env": {"HOOK_ALLOW_PATHS": "0"}}' > "$inst/settings.instance.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *HOOK_ALLOW_PATHS* && "$out" == *settings.instance.json* \
     && ! -e "$h/.claude/settings.json" ]] && no_links "$h"; then
    pass "refuses a HOOK_ALLOW_* key in settings.instance.json's env, whatever its value"
  else
    fail "refuses a HOOK_ALLOW_* key in settings.instance.json's env, whatever its value" "rc=$rc"; echo "$out" | tail -5
  fi
}

# An env that is not an object cannot be checked for overrides, so it is refused
# by name rather than left to fail somewhere later.
check_refuses_env_that_is_not_an_object() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  echo '{"env": ["HOOK_ALLOW_SECURITY=1"]}' > "$inst/machine.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *machine.json* && "$out" == *'`env`'* && "$out" != *Traceback* \
     && ! -e "$h/.claude/settings.json" ]] && no_links "$h"; then
    pass "refuses a layer whose env is not an object, naming the layer"
  else
    fail "refuses a layer whose env is not an object, naming the layer" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_registry_failure_installs_nothing() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  python3 - "$repo" "$inst" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tests.trimtab.instance_fixture import add_rule
add_rule(Path(sys.argv[2]), "Duplicate.md", "TST")
PY
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *TST* ]] && no_links "$h" \
     && [[ ! -e "$h/share/trimtab/core/.installed.json" && ! -e "$inst/machine.json" ]]; then
    pass "a registry failure installs nothing, records nothing and creates no machine.json"
  else
    fail "a registry failure installs nothing, records nothing and creates no machine.json" "rc=$rc"; echo "$out" | tail -8
  fi
}

check_flags_relative_pattern_path() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  python3 - "$h/.claude/settings.json" <<'PY' || true
import json, sys
p = sys.argv[1]
s = json.load(open(p))
s["env"]["TRIMTAB_SECRET_PATTERNS"] = "guards/secrets.patterns"
json.dump(s, open(p, "w"), indent=2)
PY
  boot "$h" "$live" --instance "$inst" --check
  if [[ $rc -eq 1 && "$out" == *TRIMTAB_SECRET_PATTERNS* ]]; then
    pass "--check flags a relative pattern path"
  else
    fail "--check flags a relative pattern path" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_idempotent() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" != *→* ]]; then
    pass "a second run changes nothing"
  else
    fail "a second run changes nothing" "rc=$rc"; echo "$out" | grep '→' || true
  fi
}

# A store holding three snapshots, each a clone of this checkout with the
# working tree's installer overlaid (so the run under test is the code under
# test), renamed to its own HEAD. Sets $snap_old, $snap_mid, $snap_new.
# make_store <store directory>
make_store() {
  local store="$1" i tmp sha
  mkdir -p "$store"
  local names=()
  for i in 1 2 3; do
    tmp="$store/tmp$i"
    git clone -q "$repo" "$tmp"
    cp "$repo/bootstrap.sh" "$tmp/bootstrap.sh"
    git -C "$tmp" add -A
    git -C "$tmp" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "snapshot $i"
    sha=$(git -C "$tmp" rev-parse HEAD)
    mv "$tmp" "$store/$sha"
    names+=("$sha")
  done
  snap_old="$store/${names[0]}"; snap_mid="$store/${names[1]}"; snap_new="$store/${names[2]}"
  echo "notes" > "$store/notes.txt"
  mkdir "$store/keep-me"
  printf '{"instance_root": "x", "instance_sha": null, "base_sha": "%s"}' "${names[0]}" > "$store/.installed.json"
}

check_retention_keeps_live_and_predecessor() {
  local h inst store
  h=$(new_home); inst=$(inst_of "$h"); store="$h/share/trimtab/core"
  make_store "$store"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && -d "$snap_new" && -d "$snap_old" && ! -e "$snap_mid" \
     && "$out" == *"${snap_mid##*/}"* && -f "$store/notes.txt" && -d "$store/keep-me" \
     && "$(printf '%s\n' "$out" | sed '/^$/d' | tail -1)" == "done. Run 'claude'"* \
     && "$out" != *"NOT GREEN"* ]]; then
    pass "retention keeps the live snapshot and its predecessor, and ignores anything else"
  else
    fail "retention keeps the live snapshot and its predecessor, and ignores anything else" "rc=$rc"; echo "$out" | tail -6
  fi
}

check_rerun_from_same_base_keeps_predecessor() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && -d "$snap_new" && -d "$snap_old" && ! -e "$snap_mid" ]]; then
    pass "a re-run from the same base keeps the predecessor"
  else
    fail "a re-run from the same base keeps the predecessor" "rc=$rc"; echo "$out" | tail -6
  fi
}

# A sandboxed run (a config dir that is not the live one) with no store of its
# own would reach the live install's store through HOME. HOME is faked here so
# a failure cannot touch the operator's real store.
check_sandbox_without_own_store_leaves_store_alone() {
  local h inst store before userbase
  h=$(new_home); inst=$(inst_of "$h")
  mkdir -p "$h/fakehome"
  store="$h/fakehome/.local/share/trimtab/core"
  make_store "$store"
  before=$(md5sum < "$store/.installed.json")
  # PYTHONUSERBASE keeps a user-site PyYAML visible under the faked HOME. It is
  # read first: a substitution inside the assignment list below would already
  # see the faked HOME.
  userbase=$(python3 -m site --user-base)
  set +e
  out=$(HOME="$h/fakehome" PYTHONUSERBASE="$userbase" \
        CLAUDE_CONFIG_DIR="$h/sandbox-config" env -u XDG_DATA_HOME \
        "$snap_new/bootstrap.sh" --no-timer --allow-worktree --instance "$inst" 2>&1)
  rc=$?
  set -e
  if [[ $rc -eq 0 && -d "$snap_mid" && -d "$snap_old" \
     && "$(md5sum < "$store/.installed.json")" == "$before" ]]; then
    pass "a sandboxed run without its own XDG_DATA_HOME leaves the store and record alone"
  else
    fail "a sandboxed run without its own XDG_DATA_HOME leaves the store and record alone" "rc=$rc"; echo "$out" | tail -6
  fi
}

# The observer probe notes a failure instead of dying, so a run can finish with
# a step that was not green. That run is still an install: it records what it
# installed. It is not a licence to delete anything, and it says so.
check_observer_failure_records_but_withholds_retention() {
  local h inst store
  h=$(new_home); inst=$(inst_of "$h"); store="$h/share/trimtab/core"
  make_store "$store"
  printf '#!/usr/bin/env bash\nexit 1\n' > "$snap_new/hooks/rule-usage.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && -d "$snap_mid" && -d "$snap_old" && "$out" == *"retention withheld"* \
        && "$(printf '%s\n' "$out" | sed '/^$/d' | tail -1)" == "NOT GREEN: observer"* \
        && "$out" != *"done. Run 'claude'"* ]] \
     && grep -q "\"base_sha\": \"${snap_new##*/}\"" "$store/.installed.json"; then
    pass "a failed observer probe still records the install, withholds retention and ends NOT GREEN"
  else
    fail "a failed observer probe still records the install, withholds retention and ends NOT GREEN" "rc=$rc"; echo "$out" | tail -6
  fi
}

# guard-mcp answers "ask" instead of blocking, so it has its own self-probe. A
# guard-mcp that stays silent on an MCP write lets every write run unprompted:
# the install must refuse it, not report done.
check_silent_guard_mcp_fails_the_install() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  printf '#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n' > "$snap_new/hooks/guard-mcp.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -ne 0 && "$out" == *"guard-mcp returned 0 without asking about an MCP write"* ]]; then
    pass "a guard-mcp that does not ask fails the install"
  else
    fail "a guard-mcp that does not ask fails the install" "rc=$rc"; echo "$out" | tail -6
  fi
}

# guard-mcp must deny set_monitor for a PR the session did not create
# (ADR 0028). A guard that passes it by name, as before that record, lets
# Auto-fix reach any PR the app binds: the install must refuse it. The stub
# passes set_monitor whole and hands every other call to the real guard, so the
# earlier guard-mcp probes still see a guard that asks.
check_set_monitor_by_name_fails_the_install() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  mv "$snap_new/hooks/guard-mcp.sh" "$snap_new/hooks/guard-mcp-real.sh"
  printf '#!/usr/bin/env bash\np=$(cat)\ncase "$p" in *mcp__ccd_pr__set_monitor*) exit 0 ;; esac\nprintf "%%s" "$p" | "${BASH_SOURCE[0]%%/*}/guard-mcp-real.sh"\n' \
    > "$snap_new/hooks/guard-mcp.sh"
  chmod +x "$snap_new/hooks/guard-mcp.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -ne 0 && "$out" == *"guard-mcp asks about an MCP write"* \
     && "$out" == *"guard-mcp did not deny set_monitor for another PR"* ]]; then
    pass "a guard-mcp that passes set_monitor by name fails the install"
  else
    fail "a guard-mcp that passes set_monitor by name fails the install" "rc=$rc"; echo "$out" | tail -6
  fi
}

# ...and a correct install proves the pair end to end: the recorder's create
# passes, another PR and another session are denied.
check_set_monitor_probe_reports_green() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"guard-mcp denies set_monitor for a PR the session did not create"* \
     && "$out" == *"guard-mcp denies set_monitor for a PR another session created"* \
     && "$out" == *"guard-mcp passes set_monitor for a PR the recorder saw the session create"* ]]; then
    pass "an install proves set_monitor passes only for the session's own PR"
  else
    fail "an install proves set_monitor passes only for the session's own PR" "rc=$rc"; echo "$out" | grep -i set_monitor | tail -6
  fi
}

# guard-mcp must ask on a PR edit that marks a draft ready (ADR 0031). A guard
# that passes update_pull_request by name loses draft-until-final through the
# MCP route: the install must refuse it. The stub passes that tool whole and
# hands every other call to the real guard, as for set_monitor above.
check_pr_edit_by_name_fails_the_install() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  mv "$snap_new/hooks/guard-mcp.sh" "$snap_new/hooks/guard-mcp-real.sh"
  printf '#!/usr/bin/env bash\np=$(cat)\ncase "$p" in *mcp__github__update_pull_request*) exit 0 ;; esac\nprintf "%%s" "$p" | "${BASH_SOURCE[0]%%/*}/guard-mcp-real.sh"\n' \
    > "$snap_new/hooks/guard-mcp.sh"
  chmod +x "$snap_new/hooks/guard-mcp.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -ne 0 && "$out" == *"guard-mcp asks about an MCP write"* \
     && "$out" == *"without asking about a PR edit that marks a draft ready"* ]]; then
    pass "a guard-mcp that passes update_pull_request by name fails the install"
  else
    fail "a guard-mcp that passes update_pull_request by name fails the install" "rc=$rc"; echo "$out" | tail -6
  fi
}

# ...and a correct install reports both: the ready-marking edit asks, a title
# edit passes.
check_pr_edit_probe_reports_green() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"guard-mcp asks about a PR edit that marks a draft ready"* \
     && "$out" == *"guard-mcp passes a PR title edit"* ]]; then
    pass "an install proves a PR edit passes only for title, body or reviewers"
  else
    fail "an install proves a PR edit passes only for title, body or reviewers" "rc=$rc"; echo "$out" | grep -i "PR edit\|title edit" | tail -6
  fi
}

# pr-body-check fails open by design, so a broken one is silent: every PR would
# open without the harness block. It is policy with CI as the gate, so the run
# installs but ends NOT GREEN and deletes nothing, as for the observer.
check_silent_pr_body_check_ends_not_green() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  printf '#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n' > "$snap_new/hooks/pr-body-check.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && -d "$snap_mid" && "$out" == *"pr-body-check returned 0, expected 2"* \
        && "$(printf '%s\n' "$out" | sed '/^$/d' | tail -1)" == "NOT GREEN: pr-body-check"* ]]; then
    pass "a pr-body-check that never blocks installs but ends NOT GREEN and withholds retention"
  else
    fail "a pr-body-check that never blocks installs but ends NOT GREEN and withholds retention" "rc=$rc"; echo "$out" | tail -6
  fi
}

# The GUARDS OFF liveness check is the only warning a session gets that its
# guards are missing (ADR 0004). A base whose check never fires must not install.
check_silent_liveness_check_fails_the_install() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  python3 - "$snap_new/settings.base.json" <<'PY2'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
for e in d["hooks"]["SessionStart"]:
    for h in e["hooks"]:
        if "GUARDS OFF" in h["command"]:
            h["command"] = "m=''; [ -z \"$m\" ] || echo 'GUARDS OFF'; exit 0"
json.dump(d, open(p, "w"), indent=2)
PY2
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -ne 0 && "$out" == *"the liveness check stayed silent with no guards installed"* ]]; then
    pass "a liveness check that never reports GUARDS OFF fails the install"
  else
    fail "a liveness check that never reports GUARDS OFF fails the install" "rc=$rc"; echo "$out" | tail -6
  fi
}

# The other half: a check that always fires would warn GUARDS OFF in every
# session, and an operator learns to ignore it.
check_liveness_check_that_always_fires_fails_the_install() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  python3 - "$snap_new/settings.base.json" <<'PY2'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
for e in d["hooks"]["SessionStart"]:
    for h in e["hooks"]:
        if "GUARDS OFF" in h["command"]:
            h["command"] = "echo 'GUARDS OFF: missing hooks/guard-paths.sh'; exit 0"
json.dump(d, open(p, "w"), indent=2)
PY2
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -ne 0 && "$out" == *"the liveness check names an installed hook as missing"* ]]; then
    pass "a liveness check that fires on a complete install fails the install"
  else
    fail "a liveness check that fires on a complete install fails the install" "rc=$rc"; echo "$out" | tail -6
  fi
}

# A base without a non-guard hook leaves sessions guarded: the liveness check
# reads HOOKS MISSING, and the install notes it rather than refusing.
check_base_without_rule_usage_is_noted_not_refused() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  rm "$snap_new/hooks/rule-usage.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"names a non-guard hook as missing on this install"* \
        && "$out" != *"names an installed hook as missing"* ]]; then
    pass "a base without rule-usage.sh is noted by the liveness probe, not refused"
  else
    fail "a base without rule-usage.sh is noted by the liveness probe, not refused" "rc=$rc"; echo "$out" | tail -6
  fi
}

# Without a CLAUDE.md the check reads GUARDS OFF and names every missing path,
# the non-guard hook among them; that is still not a guard named missing.
check_base_without_rule_usage_and_no_claude_md_is_not_refused() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  rm "$snap_new/hooks/rule-usage.sh"
  rm -f "$inst/CLAUDE.md"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"names a non-guard hook as missing on this install"* \
        && "$out" != *"names an installed hook as missing"* ]]; then
    pass "a base without rule-usage.sh and an instance without CLAUDE.md are not refused"
  else
    fail "a base without rule-usage.sh and an instance without CLAUDE.md are not refused" "rc=$rc"; echo "$out" | tail -6
  fi
}

# doctrine-drift cannot block and exits 0, so a broken one is silent. It is
# informational: the run still installs, but ends NOT GREEN and deletes nothing.
check_broken_doctrine_drift_ends_not_green() {
  local h inst store
  h=$(new_home); inst=$(inst_of "$h"); store="$h/share/trimtab/core"
  make_store "$store"
  printf '#!/usr/bin/env bash\ncat >/dev/null\necho "Traceback (most recent call last):" >&2\nexit 0\n' \
    > "$snap_new/hooks/doctrine-drift.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && -d "$snap_mid" && "$out" == *"retention withheld"* \
        && "$(printf '%s\n' "$out" | sed '/^$/d' | tail -1)" == "NOT GREEN: doctrine-drift"* ]]; then
    pass "a broken doctrine-drift installs but ends NOT GREEN and withholds retention"
  else
    fail "a broken doctrine-drift installs but ends NOT GREEN and withholds retention" "rc=$rc"; echo "$out" | tail -6
  fi
}

# --check is what a cloud setup script runs after the install, and it must fail
# the session on the same faults. Each case installs clean, breaks one hook in
# the snapshot the live links point into, and expects --check to exit 1 naming it.
check_check_reports_a_silent_pr_body_check() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  printf '#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n' > "$snap_new/hooks/pr-body-check.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst" --check
  if [[ $rc -eq 1 && "$out" == *"pr-body-check returned 0, expected 2"* ]]; then
    pass "--check reports a pr-body-check that never blocks"
  else
    fail "--check reports a pr-body-check that never blocks" "rc=$rc"; echo "$out" | tail -6
  fi
}

check_check_reports_a_broken_doctrine_drift() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  printf '#!/usr/bin/env bash\ncat >/dev/null\necho "Traceback (most recent call last):" >&2\nexit 0\n' \
    > "$snap_new/hooks/doctrine-drift.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst" --check
  if [[ $rc -eq 1 && "$out" == *"doctrine-drift exited 0 with output on a quiet payload"* ]]; then
    pass "--check reports a broken doctrine-drift"
  else
    fail "--check reports a broken doctrine-drift" "rc=$rc"; echo "$out" | tail -6
  fi
}

check_check_reports_a_silent_liveness_check() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  python3 - "$h/.claude/settings.json" <<'PY2'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
for e in d["hooks"]["SessionStart"]:
    for h in e["hooks"]:
        if "GUARDS OFF" in h["command"]:
            h["command"] = "m=''; [ -z \"$m\" ] || echo 'GUARDS OFF'; exit 0"
json.dump(d, open(p, "w"), indent=2)
PY2
  boot "$h" "$live" --instance "$inst" --check
  # Editing settings.json is drift on its own, so rc 1 does not prove the probe
  # ran: the probe's own line does, and its pass line must be absent.
  if [[ $rc -eq 1 && "$out" == *"the liveness check stayed silent with no guards installed"* \
        && "$out" != *"the liveness check reports GUARDS OFF when the guards are missing"* ]]; then
    pass "--check reports a liveness check that never fires"
  else
    fail "--check reports a liveness check that never fires" "rc=$rc"; echo "$out" | tail -6
  fi
}

# The probe payloads carry the temp directory's path; a quote or a backslash in
# it must not make them unparseable (pr-body-check would then fail open).
check_probes_accept_a_tmpdir_with_json_metacharacters() {
  local h inst t
  h=$(new_home); inst=$(inst_of "$h"); t="$h/tmp\"q\\b"
  mkdir -p "$t"
  TMPDIR="$t" boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"pr-body-check blocks a PR body"* && "$out" != *"NOT GREEN"* ]]; then
    pass "the hook probes accept a TMPDIR holding a quote and a backslash"
  else
    fail "the hook probes accept a TMPDIR holding a quote and a backslash" "rc=$rc"; echo "$out" | tail -6
  fi
}

# GIT_CEILING_DIRECTORIES is colon-separated, so a TMPDIR holding a colon cannot
# fence git; the probes then run under /tmp, and the install stays green.
check_probes_accept_a_tmpdir_with_a_colon() {
  local h inst t
  h=$(new_home); inst=$(inst_of "$h"); t="$h/tmp:x"
  mkdir -p "$t"
  TMPDIR="$t" boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"pr-body-check blocks a PR body"* \
        && "$out" == *"doctrine-drift runs cleanly"* && "$out" != *"NOT GREEN"* ]]; then
    pass "the hook probes accept a TMPDIR holding a colon"
  else
    fail "the hook probes accept a TMPDIR holding a colon" "rc=$rc"; echo "$out" | tail -6
  fi
}

# A TMPDIR inside an adopted project puts the probe directories under its
# .claude/trimtab.json; without the git fence the probes would read that
# project's harness instead of an empty one, and the install would end NOT GREEN.
check_probes_ignore_a_tmpdir_inside_an_adopted_project() {
  local h inst p
  h=$(new_home); inst=$(inst_of "$h"); p="$h/adopted"
  mkdir -p "$p/.claude" "$p/tmp"
  git -C "$p" init -q
  printf '{}\n' > "$p/.claude/trimtab.json"
  TMPDIR="$p/tmp" boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"doctrine-drift runs cleanly through the symlink"* \
        && "$out" == *"pr-body-check blocks a PR body"* && "$out" != *"NOT GREEN"* ]]; then
    pass "the hook probes ignore a TMPDIR inside an adopted project"
  else
    fail "the hook probes ignore a TMPDIR inside an adopted project" "rc=$rc"; echo "$out" | tail -6
  fi
}

# GIT_DIR overrides GIT_CEILING_DIRECTORIES, so an exported one (bootstrap run
# from a git hook or alias) would hand the probes that repository's lock.
check_probes_ignore_an_exported_git_dir() {
  local h inst p
  h=$(new_home); inst=$(inst_of "$h"); p="$h/adopted"
  mkdir -p "$p/.claude"
  git -C "$p" init -q
  printf '{}\n' > "$p/.claude/trimtab.json"
  GIT_DIR="$p/.git" GIT_WORK_TREE="$p" boot "$h" "$live" --instance "$inst"
  if [[ "$out" == *"doctrine-drift runs cleanly through the symlink"* \
        && "$out" == *"pr-body-check blocks a PR body"* ]]; then
    pass "the hook probes ignore an exported GIT_DIR and GIT_WORK_TREE"
  else
    fail "the hook probes ignore an exported GIT_DIR and GIT_WORK_TREE" "rc=$rc"; echo "$out" | tail -6
  fi
}

# Stderr that is not the hook's fault must not make a working doctrine-drift
# red; a traceback still does (check_broken_doctrine_drift_ends_not_green).
check_doctrine_drift_with_benign_stderr_stays_green() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  printf '#!/usr/bin/env bash\ncat >/dev/null\necho "x.py:1: DeprecationWarning: y" >&2\nexit 0\n' \
    > "$snap_new/hooks/doctrine-drift.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"doctrine-drift runs cleanly through the symlink"* && "$out" != *"NOT GREEN"* ]]; then
    pass "a doctrine-drift that only warns on stderr stays green"
  else
    fail "a doctrine-drift that only warns on stderr stays green" "rc=$rc"; echo "$out" | tail -6
  fi
}

# A probe whose temp directory cannot be made would run the hook in the
# checkout with git unfenced; the run must stop and say so, not probe there.
check_probe_without_a_temp_dir_stops_and_says_so() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  TMPDIR="$h/no-such-dir" boot "$h" "$live" --instance "$inst"
  if [[ $rc -ne 0 && "$out" == *"could not create a temp directory for the pr-body-check probe"* \
        && "$out" != *"pr-body-check blocks a PR body"* ]]; then
    pass "a probe without a temp directory stops the run and names it"
  else
    fail "a probe without a temp directory stops the run and names it" "rc=$rc"; echo "$out" | tail -6
  fi
}

# The same for the liveness probe. Under --check a missing pr-body-check is
# only noted, so the run reaches the liveness probe with no temp directory.
check_liveness_probe_without_a_temp_dir_stops_and_says_so() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  rm "$snap_new/hooks/pr-body-check.sh"
  TMPDIR="$h/no-such-dir" boot "$h" "$snap_new/bootstrap.sh" --instance "$inst" --check
  if [[ $rc -ne 0 && "$out" == *"could not create a temp directory for the liveness-check probe"* ]]; then
    pass "a liveness probe without a temp directory stops the run and names it"
  else
    fail "a liveness probe without a temp directory stops the run and names it" "rc=$rc"; echo "$out" | tail -6
  fi
}

# A complete install proves all three end to end.
check_install_probes_the_unprobed_hooks() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 \
     && "$out" == *"pr-body-check blocks a PR body without the harness block through the symlink"* \
     && "$out" == *"the liveness check reports GUARDS OFF when the guards are missing"* \
     && "$out" == *"the liveness check names no hook as missing on this install"* \
     && "$out" == *"doctrine-drift runs cleanly through the symlink"* ]]; then
    pass "an install probes pr-body-check, the liveness check and doctrine-drift"
  else
    fail "an install probes pr-body-check, the liveness check and doctrine-drift" "rc=$rc"; echo "$out" | tail -8
  fi
}

# Sessions reach the CLI by name only through session-path.sh (ADR 0018). An
# install whose hook writes nothing usable would start sessions, routines among
# them, that fail every method's first call: the install must refuse, and say so.
check_cli_reaches_sessions_by_name() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"session-path puts the installed CLI first on PATH through the symlink"* ]]; then
    pass "an install proves the CLI reaches sessions by name"
  else
    fail "an install proves the CLI reaches sessions by name" "rc=$rc"; echo "$out" | tail -6
  fi
}

# The hook trims a trailing / from the config dir, so the probe must compare
# against the same trimmed spelling, or a healthy install dies.
check_cli_probe_accepts_a_config_dir_with_a_trailing_slash() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  set +e
  out=$(CLAUDE_CONFIG_DIR="$h/.claude/" XDG_DATA_HOME="$h/share" "$live" --no-timer --allow-worktree --instance "$inst" 2>&1)
  rc=$?
  set -e
  if [[ $rc -eq 0 && "$out" == *"session-path puts the installed CLI first on PATH through the symlink"* ]]; then
    pass "the CLI probe accepts a config dir spelt with a trailing slash"
  else
    fail "the CLI probe accepts a config dir spelt with a trailing slash" "rc=$rc"; echo "$out" | tail -6
  fi
}

check_silent_session_path_fails_the_install() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  printf '#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n' > "$snap_new/hooks/session-path.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -ne 0 && "$out" == *"session-path did not put"*"sessions could not run trimtab"* ]]; then
    pass "a session-path hook that writes nothing fails the install"
  else
    fail "a session-path hook that writes nothing fails the install" "rc=$rc"; echo "$out" | tail -6
  fi
}

# A unit linked into a snapshot dangles when the snapshot goes, and the timer
# silently never runs; --no-timer installs do not relink it.
check_retention_spares_a_snapshot_systemd_links_into() {
  local h inst unitdir
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  unitdir="$h/xdg-config/systemd/user"
  mkdir -p "$unitdir"
  ln -s "$snap_mid/systemd/rule-usage-report.timer" "$unitdir/rule-usage-report.timer"
  XDG_CONFIG_HOME="$h/xdg-config" boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && -d "$snap_mid" && -d "$snap_old" && -d "$snap_new" ]]; then
    pass "retention spares a snapshot a systemd unit links into"
  else
    fail "retention spares a snapshot a systemd unit links into" "rc=$rc"; echo "$out" | tail -6
  fi
}

# Without PyYAML the registry check cannot run; that is a missing dependency,
# not a doctrine failure, and the message must say which.
check_missing_pyyaml_is_named() {
  local h inst stub
  h=$(new_home); inst=$(inst_of "$h")
  # A stub that fails to import shadows PyYAML wherever it is installed, so the
  # check runs on every machine and not only on ones that lack the library.
  stub="$h/no-yaml"; mkdir -p "$stub"
  echo 'raise ImportError("PyYAML hidden for this test")' > "$stub/yaml.py"
  set +e
  out=$(PYTHONPATH="$stub" CLAUDE_CONFIG_DIR="$h/sandbox-config" XDG_DATA_HOME="$h/share" \
        "$live" --no-timer --allow-worktree --instance "$inst" 2>&1)
  rc=$?
  set -e
  if [[ $rc -eq 1 && "$out" == *PyYAML* && "$out" != *"fails the registry check"* ]]; then
    pass "a missing PyYAML is named as such, not reported as a doctrine failure"
  else
    fail "a missing PyYAML is named as such, not reported as a doctrine failure" "rc=$rc"; echo "$out" | tail -4
  fi
}

# An instance with no CLAUDE.md must not leave the previous tree's link in
# place: it keeps feeding the old instructions to every session. But only a link
# an install could have made goes: one that dangles, or that points into the
# snapshot store or the last installed instance. Anything else is the operator's.
check_instance_without_claude_md_drops_a_dangling_link() {
  local h inst check_rc check_out
  h=$(new_home); inst=$(inst_of "$h")
  rm -f "$inst/CLAUDE.md"
  mkdir -p "$h/.claude"
  ln -s "$h/old-tree/CLAUDE.md" "$h/.claude/CLAUDE.md"
  boot "$h" "$live" --instance "$inst" --check
  check_rc=$rc; check_out=$out
  boot "$h" "$live" --instance "$inst"
  if [[ $check_rc -eq 1 && "$check_out" == *CLAUDE.md* && $rc -eq 0 \
     && ! -e "$h/.claude/CLAUDE.md" && ! -L "$h/.claude/CLAUDE.md" ]]; then
    pass "an instance without a CLAUDE.md drops a dangling link (and --check reports it)"
  else
    fail "an instance without a CLAUDE.md drops a dangling link (and --check reports it)" "check rc=$check_rc, rc=$rc"
  fi
}

check_instance_without_claude_md_drops_a_link_into_the_store() {
  local h inst snap
  h=$(new_home); inst=$(inst_of "$h")
  rm -f "$inst/CLAUDE.md"
  snap="$h/share/trimtab/core/$(printf '%040d' 7)"
  mkdir -p "$snap" "$h/.claude"
  echo "old instructions" > "$snap/CLAUDE.md"
  ln -s "$snap/CLAUDE.md" "$h/.claude/CLAUDE.md"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && ! -L "$h/.claude/CLAUDE.md" ]]; then
    pass "an instance without a CLAUDE.md drops a link into the snapshot store"
  else
    fail "an instance without a CLAUDE.md drops a link into the snapshot store" "rc=$rc"
  fi
}

check_instance_without_claude_md_keeps_a_link_it_did_not_make() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  rm -f "$inst/CLAUDE.md"
  mkdir -p "$h/.claude"
  echo "my own instructions" > "$h/mine.md"
  ln -s "$h/mine.md" "$h/.claude/CLAUDE.md"
  boot "$h" "$live" --instance "$inst" --check
  local check_out=$out
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$(readlink "$h/.claude/CLAUDE.md")" == "$h/mine.md" \
     && "$check_out" == *"left alone"* ]]; then
    pass "an instance without a CLAUDE.md leaves an operator's own link alone, and says so"
  else
    fail "an instance without a CLAUDE.md leaves an operator's own link alone, and says so" "rc=$rc"
  fi
}

# The checks that read the generated settings back (pack switch, private
# pattern file, the relative-path note) must not turn an unreadable file into an
# empty value: that would skip a probe, or widen "none" to every pack, silently.
check_unreadable_live_settings_are_reported() {
  local h inst first
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  echo '{not json' > "$h/.claude/settings.json"
  boot "$h" "$live" --instance "$inst" --check
  first=$out
  echo '{"env": 5}' > "$h/.claude/settings.json"
  boot "$h" "$live" --instance "$inst" --check
  # Reported once, and the probes that depend on the file are skipped rather
  # than run against "every pack" (which a switch of "none" never asked for).
  if [[ "$(grep -c 'cannot be read as settings' <<<"$first")" -eq 1 \
     && "$(grep -c 'cannot be read as settings' <<<"$out")" -eq 1 \
     && "$first" != *"sample through the symlink"* && "$out" != *"sample through the symlink"* ]]; then
    pass "an unreadable live settings.json is reported once, and its dependent probes are skipped"
  else
    fail "an unreadable live settings.json is reported once, and its dependent probes are skipped"
  fi
}

# When bootstrap clears an owned key it must say so, not that it "wrote None".
check_clearing_an_owned_key_is_reported_as_removal() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  echo '{"env": {"TRIMTAB_SECRET_PACKS": "zoho"}}' > "$inst/settings.instance.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"TRIMTAB_SECRET_PACKS"*"removed"* && "$out" != *"wrote None"* \
     && "$(settings_get "$h" env.TRIMTAB_SECRET_PACKS)" == "<absent>" ]]; then
    pass "clearing an owned key is reported as a removal"
  else
    fail "clearing an owned key is reported as a removal" "rc=$rc"; echo "$out" | grep TRIMTAB_SECRET_PACKS
  fi
}

# A snapshot that cannot be deleted must stop the run loudly: a failed rm in an
# && list does not trip errexit, and the store would grow with no signal.
check_failed_retention_delete_is_loud() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  if [[ $(id -u) -eq 0 ]]; then
    echo "SKIP  a failed retention delete is loud (running as root: permissions do not stop rm)"
    return
  fi
  make_store "$h/share/trimtab/core"
  chmod a-w "$snap_mid"          # its contents can no longer be unlinked
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  chmod u+w "$snap_mid"          # so the test's own cleanup can remove it
  if [[ $rc -eq 1 && "$out" == *"${snap_mid##*/}"* && "$out" == *"could not delete"* ]]; then
    pass "a failed retention delete stops the run and names the snapshot"
  else
    fail "a failed retention delete stops the run and names the snapshot" "rc=$rc"; echo "$out" | tail -4
  fi
}

# The record holds HEAD at install time. If that commit is gone (a rebase, a
# force-push, a gc), the registry check falls back to the committed file with a
# note instead of failing, so a stale baseline cannot block every later install.
# This pins that tolerance; it is not a fix, the behaviour already holds.
check_unreachable_recorded_instance_sha_does_not_block() {
  local h inst store
  h=$(new_home); inst=$(inst_of "$h"); store="$h/share/trimtab/core"
  mkdir -p "$store"
  printf '{"instance_root": "%s", "instance_sha": "%s", "base_sha": null, "prev_base_sha": null}' \
    "$inst" "$(printf 'a%.0s' $(seq 40))" > "$store/.installed.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" == *"doctrine registry check passed"* ]]; then
    pass "an unreachable recorded instance SHA does not block the install"
  else
    fail "an unreachable recorded instance SHA does not block the install" "rc=$rc"; echo "$out" | tail -4
  fi
}

# With CDPATH set, cd searches it for a relative argument and prints what it
# found, so INSTANCE would be two lines, or a different directory altogether. A
# decoy of the same name sits where CDPATH points; the argument means "here".
check_relative_instance_with_cdpath() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  mkdir -p "$work/cdpath-root/instance/rules"
  set +e
  out=$(cd "$h" && CDPATH="$work/cdpath-root" CLAUDE_CONFIG_DIR="$h/.claude" XDG_DATA_HOME="$h/share" \
        "$live" --no-timer --allow-worktree --instance instance 2>&1)
  rc=$?
  set -e
  if [[ $rc -eq 0 && "$(settings_get "$h" env.TRIMTAB_INSTANCE)" == "$inst" ]]; then
    pass "a relative --instance resolves to one path even with CDPATH set"
  else
    fail "a relative --instance resolves to one path even with CDPATH set" "rc=$rc"; echo "$out" | tail -4
  fi
}

# Only a snapshot in the store is something retention may keep or drop. An install
# from a plain checkout must not move the record's base, or the next snapshot
# install would treat the checkout as the predecessor and drop the real one.
check_checkout_install_leaves_the_record_base_alone() {
  local h inst store base_a base_b
  h=$(new_home); inst=$(inst_of "$h"); store="$h/share/trimtab/core"
  base_a=$(printf 'a%.0s' $(seq 40)); base_b=$(printf 'b%.0s' $(seq 40))
  mkdir -p "$store"
  printf '{"instance_root": "%s", "instance_sha": null, "base_sha": "%s", "prev_base_sha": "%s"}' \
    "$inst" "$base_a" "$base_b" > "$store/.installed.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 ]] && python3 - "$store/.installed.json" "$base_a" "$base_b" <<'PY'
import json, sys
rec = json.load(open(sys.argv[1]))
assert rec["base_sha"] == sys.argv[2] and rec["prev_base_sha"] == sys.argv[3], rec
assert isinstance(rec["instance_sha"], str) and len(rec["instance_sha"]) == 40, rec
PY
  then
    pass "an install from a plain checkout leaves the record's base and predecessor alone"
  else
    fail "an install from a plain checkout leaves the record's base and predecessor alone" "rc=$rc"
  fi
}

check_check_is_the_retention_dry_run() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst" --check
  if [[ -d "$snap_mid" && "$out" == *"would delete"*"${snap_mid##*/}"* ]]; then
    pass "--check lists the snapshots retention would delete and deletes none"
  else
    fail "--check lists the snapshots retention would delete and deletes none" "rc=$rc"; echo "$out" | tail -6
  fi
}

check_red_run_deletes_nothing() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  make_store "$h/share/trimtab/core"
  echo '{"disableAllHooks": true}' > "$inst/machine.json"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 1 && -d "$snap_mid" && -d "$snap_old" ]]; then
    pass "a refused run deletes no snapshot"
  else
    fail "a refused run deletes no snapshot" "rc=$rc"
  fi
}

# The base installs an instance, never itself: a run without --instance is a
# usage error, and it stops before it creates anything.
check_no_instance_is_refused() {
  local h err
  h=$(new_home)
  set +e
  err=$(CLAUDE_CONFIG_DIR="$h/.claude" XDG_DATA_HOME="$h/share" \
        "$live" --no-timer --allow-worktree 2>&1 >/dev/null)
  rc=$?
  set -e
  if [[ $rc -eq 64 && "$err" == *--instance* && ! -e "$h/.claude" && ! -L "$h/.claude" ]]; then
    pass "without --instance: refused as a usage error, naming --instance, creating nothing"
  else
    fail "without --instance: refused as a usage error, naming --instance, creating nothing" "rc=$rc"; echo "$err" | tail -5
  fi
}

# The observer self-probe measures a file in the checkout. If that file is one
# the checkout does not have (trimtab-core ships no CLAUDE.md), the probe notes
# a failure on every run and bootstrap is never idempotent.
check_observer_probe() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  boot "$h" "$live" --instance "$inst"
  if [[ "$out" == *"rule-usage observer wrote no usable record"* ]]; then
    fail "rule-usage observer probe records in this checkout"
  else
    pass "rule-usage observer probe records in this checkout"
  fi
}

# A python3 on PATH that reports <version> to the installer's floor probe and
# hands every other call to the real interpreter, so only the version is faked.
# Prints the directory to put first on PATH.
fake_python3() {
  local version="$1" dir real
  dir=$(mktemp -d -p "$work" fakepy.XXXXXX)
  real=$(command -v python3)
  cat > "$dir/python3" <<EOF
#!/usr/bin/env bash
if [[ "\${2-}" == *"version_info >= (3, 11)"* ]]; then
  echo "$version"
  [[ "$version" == 3.1[1-9]* ]]
  exit \$?
fi
exec "$real" "\$@"
EOF
  chmod +x "$dir/python3"
  echo "$dir"
}

# What an install or a --check would leave behind: nothing under the config
# dir, no install record, and no machine.json in the instance.
nothing_written() {
  local h="$1" inst="$2"
  [[ ! -e "$h/.claude" && ! -L "$h/.claude" && ! -e "$h/share" && ! -e "$inst/machine.json" ]]
}

check_refuses_python_older_than_3_11() {
  local h inst fake
  h=$(new_home); inst=$(inst_of "$h"); fake=$(fake_python3 3.10.4)
  PATH="$fake:$PATH" boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *"python3 3.10.4 is older than 3.11"* ]] && nothing_written "$h" "$inst"; then
    pass "refuses python3 older than 3.11, naming the version, before anything is written"
  else
    fail "refuses python3 older than 3.11, naming the version, before anything is written" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_refuses_python_older_than_3_11_under_check() {
  local h inst fake
  h=$(new_home); inst=$(inst_of "$h"); fake=$(fake_python3 3.9.18)
  PATH="$fake:$PATH" boot "$h" "$live" --instance "$inst" --check
  if [[ $rc -eq 1 && "$out" == *"python3 3.9.18 is older than 3.11"* && "$out" != *"drift detected"* ]] \
     && nothing_written "$h" "$inst"; then
    pass "--check refuses python3 older than 3.11 too, and writes nothing"
  else
    fail "--check refuses python3 older than 3.11 too, and writes nothing" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_accepts_python_3_11() {
  local h inst fake
  h=$(new_home); inst=$(inst_of "$h"); fake=$(fake_python3 3.11.0)
  PATH="$fake:$PATH" boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 0 && "$out" != *"older than 3.11"* && -L "$h/.claude/hooks" ]]; then
    pass "python3 3.11 passes the floor probe and installs"
  else
    fail "python3 3.11 passes the floor probe and installs" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_refuses_python_that_reports_no_version() {
  local h inst dir
  h=$(new_home); inst=$(inst_of "$h")
  dir=$(mktemp -d -p "$work" brokenpy.XXXXXX)
  printf '#!/usr/bin/env bash\nexit 7\n' > "$dir/python3"; chmod +x "$dir/python3"
  PATH="$dir:$PATH" boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *"did not report a version"* ]] && nothing_written "$h" "$inst"; then
    pass "refuses a python3 that cannot report its version, before anything is written"
  else
    fail "refuses a python3 that cannot report its version, before anything is written" "rc=$rc"; echo "$out" | tail -5
  fi
}

check_refuses_python_older_than_3_11
check_refuses_python_older_than_3_11_under_check
check_accepts_python_3_11
check_refuses_python_that_reports_no_version
check_writes_instance_values
check_base_hooks_survive
check_installed_settings_turn_commit_and_pr_attribution_off
check_refuses_disable_all_hooks
check_refuses_a_layer_that_sets_attribution
check_attribution_refusal_leaves_a_live_install_alone
check_refuses_hooks_replaced
check_refuses_hook_allow_in_machine_env
check_refuses_hook_allow_in_instance_env
check_refuses_env_that_is_not_an_object
check_registry_failure_installs_nothing
check_flags_relative_pattern_path
check_idempotent
check_retention_keeps_live_and_predecessor
check_rerun_from_same_base_keeps_predecessor
check_sandbox_without_own_store_leaves_store_alone
check_observer_failure_records_but_withholds_retention
check_silent_guard_mcp_fails_the_install
check_set_monitor_by_name_fails_the_install
check_set_monitor_probe_reports_green
check_pr_edit_by_name_fails_the_install
check_pr_edit_probe_reports_green
check_silent_pr_body_check_ends_not_green
check_silent_liveness_check_fails_the_install
check_liveness_check_that_always_fires_fails_the_install
check_base_without_rule_usage_is_noted_not_refused
check_base_without_rule_usage_and_no_claude_md_is_not_refused
check_broken_doctrine_drift_ends_not_green
check_check_reports_a_silent_pr_body_check
check_check_reports_a_broken_doctrine_drift
check_check_reports_a_silent_liveness_check
check_probes_accept_a_tmpdir_with_json_metacharacters
check_probes_accept_a_tmpdir_with_a_colon
check_probes_ignore_a_tmpdir_inside_an_adopted_project
check_probes_ignore_an_exported_git_dir
check_doctrine_drift_with_benign_stderr_stays_green
check_probe_without_a_temp_dir_stops_and_says_so
check_liveness_probe_without_a_temp_dir_stops_and_says_so
check_install_probes_the_unprobed_hooks
check_cli_reaches_sessions_by_name
check_cli_probe_accepts_a_config_dir_with_a_trailing_slash
check_silent_session_path_fails_the_install
check_retention_spares_a_snapshot_systemd_links_into
check_missing_pyyaml_is_named
check_instance_without_claude_md_drops_a_dangling_link
check_instance_without_claude_md_drops_a_link_into_the_store
check_instance_without_claude_md_keeps_a_link_it_did_not_make
check_unreadable_live_settings_are_reported
check_clearing_an_owned_key_is_reported_as_removal
check_failed_retention_delete_is_loud
check_unreachable_recorded_instance_sha_does_not_block
check_relative_instance_with_cdpath
check_checkout_install_leaves_the_record_base_alone
check_check_is_the_retention_dry_run
check_red_run_deletes_nothing
check_no_instance_is_refused
check_observer_probe

if [[ $fails -ne 0 ]]; then
  echo "$fails check(s) FAILED"
  exit 1
fi
echo "ALL PASS"
