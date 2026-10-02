#!/usr/bin/env bash
# Bootstrap writes TRIMTAB_INSTANCE (its own checkout) into the generated
# settings, and owns that key over machine.json (stage S2, PR 2).
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
home=$(mktemp -d -t bootstrap-env.XXXXXX)
trap 'rm -rf "$home"' EXIT
CLAUDE_CONFIG_DIR="$home" "$repo/bootstrap.sh" --no-timer --allow-worktree >/dev/null
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
