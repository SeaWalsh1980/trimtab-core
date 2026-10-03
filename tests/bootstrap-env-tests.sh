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

check_refuses_hooks_replaced() {
  local h inst
  h=$(new_home); inst=$(inst_of "$h")
  echo '{"hooks": "off"}' > "$inst/settings.instance.json"
  boot "$h" "$live" --instance "$inst"
  if [[ $rc -eq 1 && "$out" == *settings.instance.json* ]] && no_links "$h"; then
    pass "refuses a layer that replaces hooks, naming the layer"
  else
    fail "refuses a layer that replaces hooks, naming the layer" "rc=$rc"; echo "$out" | tail -5
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
  if [[ $rc -eq 1 && "$out" == *TST* ]] && no_links "$h" && [[ ! -e "$h/share/trimtab/core/.installed.json" ]]; then
    pass "a registry failure installs nothing and records nothing"
  else
    fail "a registry failure installs nothing and records nothing" "rc=$rc"; echo "$out" | tail -8
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
     && "$out" == *"${snap_mid##*/}"* && -f "$store/notes.txt" && -d "$store/keep-me" ]]; then
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

check_no_instance_keeps_single_root() {
  local h
  h=$(new_home)
  boot "$h" "$live"
  if [[ $rc -eq 0 && "$out" == *"--instance will be required"* \
     && "$(settings_get "$h" env.TRIMTAB_INSTANCE)" == "$repo" ]]; then
    pass "without --instance: today's single-root install, with a notice"
  else
    fail "without --instance: today's single-root install, with a notice" "rc=$rc"; echo "$out" | tail -5
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

check_writes_instance_values
check_base_hooks_survive
check_refuses_disable_all_hooks
check_refuses_hooks_replaced
check_registry_failure_installs_nothing
check_flags_relative_pattern_path
check_idempotent
check_retention_keeps_live_and_predecessor
check_rerun_from_same_base_keeps_predecessor
check_sandbox_without_own_store_leaves_store_alone
check_check_is_the_retention_dry_run
check_red_run_deletes_nothing
check_no_instance_keeps_single_root
check_observer_probe

if [[ $fails -ne 0 ]]; then
  echo "$fails check(s) FAILED"
  exit 1
fi
echo "ALL PASS"
