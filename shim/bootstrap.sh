#!/usr/bin/env bash
# The instance's bootstrap: fetch the pinned base snapshot, verify it, and hand
# over to its installer. It holds no install logic. Copied byte for byte into
# each instance; the source is the base's shim/bootstrap.sh.
#
#   ./bootstrap.sh [--allow-worktree] [--check] [--no-timer]
#
# The only URL it builds is https://github.com/<base.repo>.git, from a name
# validated as owner/name. It never takes a URL or a clone source from the
# environment, instance.json or a flag, and it runs git in an environment it
# builds itself. A snapshot that exists but differs from its pinned commit in
# any file, or whose git directory holds anything a clone does not write, is a
# tampering signal: it stops, and repairs nothing. The one exception is
# bytecode caches (__pycache__), which running the hooks writes and which it
# deletes, printing each, before it verifies. The instance is always the directory this script sits in; a
# caller-supplied --instance is refused.
set -euo pipefail

die() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
command -v git >/dev/null 2>&1 || die "git is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

# git_safe: git in an environment built here, not inherited. Nothing of the
# caller's reaches it (no GIT_* variable, no credential helper or askpass
# program, no user or system configuration, so no url.<x>.insteadOf), except the
# proxy and CA-bundle settings a clone may need to reach GitHub at all; the SHA
# pin makes the transport untrusted anyway. It never prompts. core.fsmonitor is
# off so no command named in a snapshot's own config can run, and replace refs
# are off so the commit checked is the commit named. Python is always run with
# -I (no working directory or PYTHON* variable on its path), so nothing the
# caller leaves lying about can stand in for the validator or the verifier.
git_safe() {
  local pass=() v
  for v in HTTPS_PROXY https_proxy ALL_PROXY all_proxy NO_PROXY no_proxy \
           SSL_CERT_FILE SSL_CERT_DIR CURL_CA_BUNDLE; do
    if [[ -n "${!v+x}" ]]; then pass+=("$v=${!v}"); fi
  done
  env -i PATH="$PATH" HOME=/nonexistent LC_ALL=C GIT_TERMINAL_PROMPT=0 \
      GIT_NO_REPLACE_OBJECTS=1 \
      GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1 \
      ${pass[@]+"${pass[@]}"} \
      git -c core.fsmonitor=false "$@"
}

INSTANCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
allow_worktree=0
for arg in "$@"; do
  case "$arg" in
    --allow-worktree) allow_worktree=1 ;;
    --instance|--instance=*) die "the instance is the directory this script sits in; $arg is not accepted" ;;
  esac
done

# 1. Refuse a linked worktree: every link the installer makes would point into a
#    tree meant to be deleted (the canonical-checkout rule). A linked worktree is
#    a checkout whose own git directory is not the common one; a primary checkout,
#    a submodule and a subdirectory of either are not. Where git cannot answer for
#    a directory that has a .git, the shim refuses rather than assume. A copy that
#    is not a git checkout at all (a tarball) has nothing to refuse.
if gitdir=$(git_safe -C "$INSTANCE" rev-parse --absolute-git-dir 2>/dev/null); then
  unknown="could not tell whether $INSTANCE is a linked worktree (git did not answer); refusing, because a worktree is meant to be deleted"
  common_raw=$(git_safe -C "$INSTANCE" rev-parse --git-common-dir 2>/dev/null) || die "$unknown"
  common=$(cd -P -- "$INSTANCE" && cd -P -- "$common_raw" && pwd -P) || die "$unknown"
  gitdir=$(cd -P -- "$gitdir" && pwd -P) || die "$unknown"
  if [[ "$gitdir" != "$common" && $allow_worktree -eq 0 ]]; then
    canonical="${common%/.git}"
    die "refusing to install from a linked worktree: $INSTANCE (the canonical checkout is $canonical); pass --allow-worktree if that is genuinely what you want"
  fi
elif [[ -e "$INSTANCE/.git" ]]; then
  die "could not tell whether $INSTANCE is a linked worktree (git failed); refusing, because a worktree is meant to be deleted"
fi

# 2. Read instance.json and validate it, with the same rules as the base
#    package's instance reader (a test pins their agreement, key for key).
fields=$(python3 -I - "$INSTANCE/instance.json" <<'PY'
import json, re, sys
from pathlib import PurePosixPath
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PACK = re.compile(r"[a-z0-9-]+")
def bad(why):
    print(f"instance.json: {why}", file=sys.stderr); sys.exit(1)
def repo(value):
    return isinstance(value, str) and REPO.fullmatch(value) and not ({".", ".."} & set(value.split("/")))
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
version = data.get("schema_version")
if type(version) is not int or version != 1:
    bad("schema_version must be 1")
base = data.get("base")
if not isinstance(base, dict) or set(base) - {"repo", "sha"}:
    bad("base must be an object with repo and sha only")
for key, value in (("repo", data.get("repo")), ("base.repo", base.get("repo"))):
    if not repo(value):
        bad(f"{key} must be owner/name, never a URL, and no part may be . or ..")
sha = base.get("sha")
if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
    bad("base.sha must be a full 40-character lower-case commit SHA")
guards = data.get("guards", {})
if not isinstance(guards, dict):
    bad("guards must be an object")
for key in guards:
    if key in ("cloud", "routines"):
        bad(f"guards.{key}: no file holds routine or environment IDs; remove the key")
    if key not in ("secrets_packs", "secrets_patterns"):
        bad(f"guards.{key}: unknown key")
if "secrets_packs" in guards:
    packs = guards["secrets_packs"]
    if packs != "none":
        if not isinstance(packs, list) or not packs:
            bad('guards.secrets_packs must be a non-empty list of pack names, or "none"')
        if not all(isinstance(p, str) and PACK.fullmatch(p) and p != "none" for p in packs):
            bad("guards.secrets_packs: each pack name is lower-case letters, digits and hyphens")
if "secrets_patterns" in guards:
    path = guards["secrets_patterns"]
    if (not isinstance(path, str) or not path or path.startswith(("/", "~")) or "\\" in path
            or ".." in PurePosixPath(path).parts):
        bad("guards.secrets_patterns must be a path inside the instance, relative, with no '..' and no leading '~'")
print(base["repo"], sha)
PY
) || die "instance.json is invalid (see above); nothing was changed"
read -r base_repo base_sha <<<"$fields"
# Belt and braces: re-check in bash what reaches git and the file system.
[[ "$base_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "base.repo is not owner/name"
owner=${base_repo%%/*}; name=${base_repo#*/}
[[ "$owner" != . && "$owner" != .. && "$name" != . && "$name" != .. ]] || die "base.repo has a . or .. part"
[[ "$base_sha" =~ ^[0-9a-f]{40}$ ]] || die "base.sha is not a 40-character SHA"

# 3. Ensure the snapshot.
#
# verify_tree <dir>: every file in the working tree is exactly what the pinned
# commit holds, and nothing else is there. It reads the commit's tree and the
# files themselves and ignores the snapshot's index, ignore rules and config, so
# an excluded or assume-unchanged file cannot hide, and nothing the snapshot's
# own config names can run. Nothing is tolerated in the tree, bytecode included:
# Python runs a compiled file whose header matches, or whose hash is unchecked,
# without reading its source, so a cache directory is a place to hide code.
# Running the hooks writes such caches, and they regenerate, so the one repair
# the shim makes is to delete them first (drop_bytecode_caches), printing each,
# before it verifies. The snapshot's .git must hold no hooks.
read -r -d '' VERIFY_PY <<'PY' || true
import hashlib, os, stat, sys
root = sys.argv[1]
want = {}
for entry in sys.stdin.buffer.read().split(b"\0"):
    if not entry:
        continue
    meta, path = entry.split(b"\t", 1)
    mode, kind, sha = meta.decode().split(" ")
    if kind != "blob":
        print(f"the tree holds an entry that is not a file: {os.fsdecode(path)!r}", file=sys.stderr)
        sys.exit(1)
    want[os.fsdecode(path)] = (mode, sha)
extra, changed, seen = [], [], set()
def check(rel, full, st):
    expected = want.get(rel)
    if expected is None:
        extra.append(rel)
        return
    seen.add(rel)
    if stat.S_ISLNK(st.st_mode):
        data, mode = os.fsencode(os.readlink(full)), "120000"
    else:
        with open(full, "rb") as f:
            data = f.read()
        mode = "100755" if st.st_mode & 0o100 else "100644"
    if (mode, hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()) != expected:
        changed.append(rel)
def scan(rel):
    with os.scandir(os.path.join(root, rel) if rel else root) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        r = f"{rel}/{e.name}" if rel else e.name
        if not rel and e.name == ".git":
            continue
        st = e.stat(follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            scan(r)
        elif stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
            check(r, e.path, st)
        else:
            extra.append(r)
scan("")
missing = sorted(set(want) - seen)
if extra or changed or missing:
    for label, names in (("not in the commit", extra), ("changed", changed), ("missing", missing)):
        if names:
            shown = ", ".join(repr(n) for n in names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
            print(f"  {label}: {shown}", file=sys.stderr)
    sys.exit(1)
PY
# check_git_dir <dir>: the snapshot's own git directory holds nothing that could
# make git, run later by the installer or the tooling in the snapshot, do
# something other than read the pinned commit. The verifier below skips .git, so
# this is where it is looked at: no hooks (and the check fails closed if it
# cannot read the directory), no alternate object stores, no attributes file, a
# config limited to the keys a clone writes (so no hooksPath, filter, include or
# fsmonitor), and every reachable object re-hashed by git fsck.
check_git_dir() {
  local gd="$1/.git" found key keys f out
  if [[ -L "$gd/hooks" ]]; then
    echo "  .git/hooks is a symlink" >&2; return 1
  fi
  if [[ -e "$gd/hooks" ]]; then
    found=$(find "$gd/hooks" -mindepth 1 -print -quit) \
      || { echo "  .git/hooks could not be read" >&2; return 1; }
    [[ -z "$found" ]] || { echo "  .git/hooks is not empty" >&2; return 1; }
  fi
  for f in objects/info/alternates objects/info/http-alternates info/attributes; do
    if [[ -e "$gd/$f" || -L "$gd/$f" ]]; then
      echo "  .git/$f exists, and a snapshot never has one" >&2; return 1
    fi
  done
  keys=$(git_safe config --file "$gd/config" --list --name-only) \
    || { echo "  .git/config could not be read" >&2; return 1; }
  while IFS= read -r key; do
    case "$key" in
      "") ;;
      core.repositoryformatversion|core.filemode|core.bare|core.logallrefupdates|core.ignorecase) ;;
      core.symlinks|core.precomposeunicode|remote.origin.url|remote.origin.fetch) ;;
      branch.*.remote|branch.*.merge) ;;
      *) echo "  .git/config sets $key, which a snapshot never needs" >&2; return 1 ;;
    esac
  done <<<"$keys"
  out=$(git_safe -C "$1" fsck --no-dangling --no-progress 2>&1) \
    || { printf '%s\n' "$out" >&2; echo "  git fsck found a problem in the snapshot's objects" >&2; return 1; }
}
verify_tree() {
  check_git_dir "$1" || return 1
  git_safe -C "$1" ls-tree -r -z HEAD | python3 -I -c "$VERIFY_PY" "$1"
}
# drop_bytecode_caches <dir>: delete every __pycache__ directory in the working
# tree (never through a symlink, never inside .git), printing each.
drop_bytecode_caches() {
  local cache
  while IFS= read -r -d '' cache; do
    rm -rf -- "$cache" || die "could not delete the bytecode cache $cache"
    printf '  \033[33m-\033[0m removed bytecode cache %s\n' "$cache"
  done < <(find "$1" -path "$1/.git" -prune -o -type d -name __pycache__ -prune -print0)
}

store="${XDG_DATA_HOME:-$HOME/.local/share}/trimtab/core"
snap="$store/$base_sha"
if [[ -e "$snap" || -L "$snap" ]]; then
  [[ ! -L "$snap" && -d "$snap/.git" && ! -L "$snap/.git" ]] \
    || die "the snapshot $snap is not a plain git checkout; stopping (nothing repairs this automatically)"
  head=$(git_safe -C "$snap" rev-parse HEAD 2>/dev/null || true)
  [[ "$head" == "$base_sha" ]] || die "the snapshot $snap is at ${head:-no commit}, not the pin; stopping (nothing repairs this automatically)"
  drop_bytecode_caches "$snap"
  verify_tree "$snap" || die "the snapshot $snap differs from its pinned commit (see above); stopping (nothing repairs this automatically)"
else
  mkdir -p "$store"
  tmp=$(mktemp -d "$store/.fetch.XXXXXX")
  trap 'rm -rf -- "$tmp"' EXIT
  git_safe -C "$tmp" clone --quiet --no-checkout --template= "https://github.com/$base_repo.git" "$tmp/repo" \
    || die "could not clone the base $base_repo"
  git_safe -C "$tmp/repo" -c advice.detachedHead=false checkout --quiet "$base_sha" \
    || die "the base $base_repo has no commit $base_sha"
  [[ "$(git_safe -C "$tmp/repo" rev-parse HEAD)" == "$base_sha" ]] || die "the fetched HEAD is not the pin"
  verify_tree "$tmp/repo" || die "the fetched tree differs from the pinned commit (see above)"
  mv -T "$tmp/repo" "$snap" || die "could not move the snapshot into place"
  rm -rf -- "$tmp"; trap - EXIT
fi

# 4. Hand over.
exec "$snap/bootstrap.sh" --instance "$INSTANCE" "$@"
