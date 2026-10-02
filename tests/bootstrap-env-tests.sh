#!/usr/bin/env bash
# Bootstrap writes TRIMTAB_INSTANCE (its own checkout) into the generated
# settings, and owns that key over machine.json (stage S2, PR 2).
set -euo pipefail
# Tests the guards as they ship, whatever the caller exported: bootstrap's
# self-probe expects guard-security to block, and an inherited HOOK_ALLOW_*
# makes it return 0. Clear them all before bootstrap runs.
for v in $(compgen -v HOOK_ALLOW_); do unset "$v"; done
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
home=$(mktemp -d -t bootstrap-env.XXXXXX)
trap 'rm -rf "$home"' EXIT
out=$(CLAUDE_CONFIG_DIR="$home" "$repo/bootstrap.sh" --no-timer --allow-worktree)
python3 - "$home/settings.json" "$repo" <<'PY'
import json, sys
settings, repo = json.load(open(sys.argv[1])), sys.argv[2]
env = settings.get("env", {})
assert env.get("TRIMTAB_INSTANCE") == repo, env
# The pack switch is whatever settings.base.json declares (an instance's
# choice); in trimtab-core, whose settings.base.json declares none, it is absent.
declared = json.load(open(f"{repo}/settings.base.json")).get("env", {}).get("TRIMTAB_SECRET_PACKS")
assert env.get("TRIMTAB_SECRET_PACKS") == declared, env
print("PASS  bootstrap writes TRIMTAB_INSTANCE and the declared pack switch")
PY
# The observer self-probe measures a file in the checkout. If that file is one
# the checkout does not have (trimtab-core ships no CLAUDE.md), the probe notes
# a failure on every run and bootstrap is never idempotent.
if grep -q 'rule-usage observer wrote no usable record' <<<"$out"; then
  echo "FAIL  bootstrap's rule-usage observer probe wrote no usable record in this checkout"
  exit 1
fi
echo "PASS  bootstrap's rule-usage observer probe records in this checkout"
