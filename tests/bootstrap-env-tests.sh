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

# The observer probe notes a failure instead of dying, so a run can finish with
# a step that was not green. That run is still an install: it records what it
# installed. It is not a licence to delete anything, and it says so.
check_observer_failure_records_but_withholds_retention() {
  local h inst store
  h=$(new_home); inst=$(inst_of "$h"); store="$h/share/trimtab/core"
  make_store "$store"
  printf '#!/usr/bin/env bash\nexit 1\n' > "$snap_new/hooks/rule-usage.sh"
  boot "$h" "$snap_new/bootstrap.sh" --instance "$inst"
  if [[ $rc -eq 0 && -d "$snap_mid" && -d "$snap_old" && "$out" == *"retention withheld"* ]] \
     && grep -q "\"base_sha\": \"${snap_new##*/}\"" "$store/.installed.json"; then
    pass "a failed observer probe still records the install, withholds retention and says so"
  else
    fail "a failed observer probe still records the install, withholds retention and says so" "rc=$rc"; echo "$out" | tail -6
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

check_writes_instance_values
check_base_hooks_survive
check_refuses_disable_all_hooks
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
