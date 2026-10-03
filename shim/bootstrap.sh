#!/usr/bin/env bash
# The instance's bootstrap: fetch the pinned base snapshot, verify it, and hand
# over to its installer. It holds no install logic. Copied byte for byte into
# each instance; the source is the base's shim/bootstrap.sh.
#
#   ./bootstrap.sh [--allow-worktree] [--check] [--no-timer]
#
# The only URL it builds is https://github.com/<base.repo>.git, from a name
# validated as owner/name. It never takes a URL or a clone source from the
# environment, instance.json or a flag. A snapshot that exists but is dirty or
# at another commit is a tampering signal: it stops, and repairs nothing.
set -euo pipefail

die() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
command -v git >/dev/null 2>&1 || die "git is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

INSTANCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
allow_worktree=0
for arg in "$@"; do [[ "$arg" == --allow-worktree ]] && allow_worktree=1; done

# 1. Refuse a linked worktree: every link the installer makes would point into a
#    tree meant to be deleted (the canonical-checkout rule).
common=$(git -C "$INSTANCE" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
if [[ -n "$common" ]]; then
  canonical="${common%/}"; canonical="${canonical%/.git}"
  phys=$(cd -P "$INSTANCE" && pwd -P)
  if [[ "$canonical" != "$phys" && $allow_worktree -eq 0 ]]; then
    die "refusing to install from a linked worktree: $INSTANCE (the canonical checkout is $canonical); pass --allow-worktree if that is genuinely what you want"
  fi
fi

# 2. Read instance.json and validate what the shim needs, with the same rules as
#    the base package's instance reader (a test pins their agreement).
fields=$(python3 - "$INSTANCE/instance.json" <<'PY'
import json, re, sys
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
def bad(why):
    print(f"instance.json: {why}", file=sys.stderr); sys.exit(1)
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    bad("missing, unreadable or not JSON")
if not isinstance(data, dict):
    bad("must hold a JSON object")
for key in data:
    if key in ("cloud", "routines"):
        bad(f"{key}: no file holds routine or environment IDs; remove the key")
    if key not in ("schema_version", "repo", "base", "guards"):
        bad(f"{key}: unknown key")
if data.get("schema_version") != 1:
    bad("schema_version must be 1")
base = data.get("base")
if not isinstance(base, dict) or set(base) - {"repo", "sha"}:
    bad("base must be an object with repo and sha only")
for key, value in (("repo", data.get("repo")), ("base.repo", base.get("repo"))):
    if not isinstance(value, str) or not REPO.fullmatch(value):
        bad(f"{key} must be owner/name, never a URL")
sha = base.get("sha")
if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
    bad("base.sha must be a full 40-character lower-case commit SHA")
print(base["repo"], sha)
PY
) || die "instance.json is invalid (see above); nothing was changed"
read -r base_repo base_sha <<<"$fields"
# Belt and braces: re-check in bash what reaches git and the file system.
[[ "$base_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "base.repo is not owner/name"
[[ "$base_sha" =~ ^[0-9a-f]{40}$ ]] || die "base.sha is not a 40-character SHA"

# 3. Ensure the snapshot.
store="${XDG_DATA_HOME:-$HOME/.local/share}/trimtab/core"
snap="$store/$base_sha"
if [[ -e "$snap" ]]; then
  head=$(git -C "$snap" rev-parse HEAD 2>/dev/null || true)
  [[ "$head" == "$base_sha" ]] || die "the snapshot $snap is at ${head:-no commit}, not the pin; stopping (nothing repairs this automatically)"
  [[ -z "$(git -C "$snap" status --porcelain --untracked-files=all)" ]] \
    || die "the snapshot $snap has local changes; stopping (nothing repairs this automatically)"
else
  mkdir -p "$store"
  tmp=$(mktemp -d "$store/.fetch.XXXXXX")
  trap 'rm -rf -- "$tmp"' EXIT
  git clone --quiet --no-checkout "https://github.com/$base_repo.git" "$tmp/repo" \
    || die "could not clone the base $base_repo"
  git -C "$tmp/repo" -c advice.detachedHead=false checkout --quiet "$base_sha" \
    || die "the base $base_repo has no commit $base_sha"
  [[ "$(git -C "$tmp/repo" rev-parse HEAD)" == "$base_sha" ]] || die "the fetched HEAD is not the pin"
  [[ -z "$(git -C "$tmp/repo" status --porcelain --untracked-files=all)" ]] || die "the fetched tree is not clean"
  mv -T "$tmp/repo" "$snap" || die "could not move the snapshot into place"
  rm -rf -- "$tmp"; trap - EXIT
fi

# 4. Hand over.
exec "$snap/bootstrap.sh" --instance "$INSTANCE" "$@"
