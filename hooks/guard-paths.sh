#!/usr/bin/env bash
# PreToolUse hook, matcher: Read|Edit|Write|MultiEdit|NotebookEdit|Bash
#
# Path policy: refuses to touch protected files by location or filename.
# Exit 2 = blocked, stderr is fed back to Claude. Exit 0 = allowed.
#
# Fails CLOSED: if it cannot parse the payload, or the payload is empty, it
# blocks rather than waving the call through. Its externals: `cat` reads the
# payload and prints the deny messages (missing, the payload is empty, which
# blocks); jq or python3 parses the payload; and `readlink`, run only when
# ~/.claude/CLAUDE.md is a link (bootstrap always makes it one), without which
# every call blocks. The harness layer (ADR 0039) adds `stat`, run once per
# write to an existing file inside a repository to read its link count; a
# missing `stat` blocks that write. `sed` was dropped for bash expansions after a missing one
# was found to empty both the path and the command and allow everything. See
# docs/adr/0001-guards-fail-closed-on-missing-dependencies.md.
#
# Session override: export HOOK_ALLOW_PATHS=1
#   (must be exported before launching claude, or set in settings env —
#    an inline prefix on the Bash tool's command never reaches this process)

set -uo pipefail   # deliberately not -e: an unexpected non-zero must not
                   # short-circuit into a silent allow

[[ "${HOOK_ALLOW_PATHS:-0}" == "1" ]] && exit 0

payload=$(cat)

# An empty payload is not an empty tool call. jq reports empty stdin as a
# successful parse of nothing, so the checks below would see no path and no
# command and allow. Truncated stdin and a missing `cat` both land here.
if [[ -z "$payload" ]]; then
  echo "guard-paths: empty hook payload. Blocking (fail closed)." >&2
  exit 2
fi

# ---- parse -------------------------------------------------------------
# Emits three lines: the target path, command, then tool_name (any may be
# empty). The target path is file_path, or NotebookEdit's notebook_path, or
# path, the same order guard-security reads them in: a key left out here is a
# tool whose every path check passes on an empty string. Only a non-empty
# string counts, in both branches: jq's `//` alone kept "", 0, [] and {},
# which python3's `or` skips, and printed an array over several lines.
#
# A target path holding a newline or carriage return fails the parse, so the
# call blocks. The fields are one per line, and a newline in the path pushed
# its tail onto the later lines: a path ending "\n\nRead\n/x" was split as
# tool_name Read, skipping the control-plane check while the tool wrote
# under the live tree. Collapsing it, as the command's are below, would check
# a different path from the one the tool writes.
#
# A payload naming more than one of the three keys (any value but null) also
# fails the parse. The guard checks one path and the tool may write another,
# so a decoy file_path beside a protected notebook_path was judged on the
# decoy. Every tool sends one of them, so this costs no real call. See
# docs/adr/0033-guards-refuse-a-payload-that-names-more-than-one-path.md.
#
# The command has newlines collapsed to spaces in BOTH branches — jq's raw
# output would otherwise spread a multi-line command over several lines and
# only the first would survive the line-2 extraction below.
#
# tool_name is LAST on purpose. Lines 1 and 2 keep their meaning even if one
# branch were left stale, so skew between the jq and python3 arms degrades to
# "tool_name empty" — which enforces — rather than to "the command landed on
# the file_path line". Nothing enforces lockstep structurally; CI running the
# whole matrix twice, with jq and with jq removed, is what enforces it.
parse() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '(([.tool_input.file_path, .tool_input.notebook_path, .tool_input.path]
              | map(select(. != null))
              | if length > 1 then error("more than one path key") else . end
              | map(select(type == "string" and . != "")) | .[0] // "")
             | if test("[\n\r]") then error("line break in the target path") else . end),
           ((.tool_input.command // "") | gsub("\n"; " ")),
           ((.tool_name // "") | gsub("\n"; " "))'
  elif command -v python3 >/dev/null 2>&1; then
    # -I: isolated, so a json.py in the working directory (the repository
    # under work) or a PYTHON* variable cannot stand in for the standard library.
    python3 -I -c '
import json, sys
d = json.load(sys.stdin)
ti = d.get("tool_input", {}) or {}
keys = [k for k in (ti.get("file_path"), ti.get("notebook_path"), ti.get("path")) if k is not None]
if len(keys) > 1:
    sys.exit("more than one path key")
target = next((k for k in keys if isinstance(k, str) and k), "")
if "\n" in target or "\r" in target:
    sys.exit("line break in the target path")
print(target)
print((ti.get("command") or "").replace("\n", " "))
print((d.get("tool_name") or "").replace("\n", " "))
'
  else
    return 1
  fi
}

if ! parsed=$(printf '%s' "$payload" | parse 2>/dev/null); then
  echo "guard-paths: cannot parse payload (needs jq or python3, one target path at most, and no line break in it). Blocking." >&2
  exit 2
fi

# Split the three emitted lines with bash expansions rather than `sed -n Np`.
# sed was an unchecked dependency of a guard that documents itself as failing
# closed: absent from PATH, all three variables came out empty and every check
# below passed vacuously — the same class of silent allow as a missing grep in
# guard-secrets. Note that an empty `tool` enforces, since only Read is exempt,
# but an empty `file` and `cmd` are exactly what an allow looks like. Splitting
# the parsed fields no longer leaves bash; the externals that remain are listed
# in the header, and each one's absence blocks.
#
# Command substitution has already stripped the trailing newlines, so a payload
# whose later fields are empty arrives with fewer than three lines.
file=${parsed%%$'\n'*}
cmd=""
tool=""
if [[ "$parsed" == *$'\n'* ]]; then
  rest=${parsed#*$'\n'}
  cmd=${rest%%$'\n'*}
  if [[ "$rest" == *$'\n'* ]]; then
    tool=${rest#*$'\n'}
    tool=${tool%%$'\n'*}
  fi
fi

deny() {
  cat >&2 <<EOF
BLOCKED by guard-paths: $1
Target: $2

Do not attempt another route to this file — the shell, the file reader, a
subagent, and an editor are all guarded, and retrying wastes turns.

If the question is what configuration exists rather than what the values are,
run:  $HOME/.claude/bin/env-names.sh <path>
which prints variable names and value shapes with the values redacted, and is
not blocked.

If the actual values are genuinely needed, stop and ask the operator to read
the file and paste the specific line. Do not set HOOK_ALLOW_PATHS yourself.
EOF
  exit 2
}

# ---- live control plane ------------------------------------------------
# The guards used to be enforced by files the guards did not protect: hooks/,
# rules/, settings, bootstrap.sh. Because ~/.claude symlinks into the config
# repo, a local edit is live for the next session with no commit, no pull
# request and no CI — a shorter route than anything through GitHub.
#
# Scope is the LIVE tree only: $cp_home's config names (as spelt, and by their
# physical path when $cp_home is or sits under a link), the realpath targets of
# its symlinks, the exact repo-root files bootstrap consumes, and the base
# snapshot store
# (every snapshot in it, linked or not: the rollback snapshot is live the
# moment a rollback re-points the links at it). The roots derive from
# CLAUDE_HOME and never from this script's own location — a guard that
# protected the tree it was read from would make every worktree protect itself
# and block the pull request that fixes the guard.
#
# Residual, stated rather than hidden: `git pull` and `bootstrap.sh` change
# these same files without an Edit/Write call, so this does not cover a
# repo-level compromise. See
# docs/adr/0002-anchor-the-guards-to-the-live-control-plane.md.
if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
  cp_home="${CLAUDE_CONFIG_DIR%/}"
elif [[ -n "${HOME:-}" ]]; then
  cp_home="${HOME%/}/.claude"
else
  echo "guard-paths: neither CLAUDE_CONFIG_DIR nor HOME is set; cannot locate the control plane. Blocking." >&2
  exit 2
fi

# `cd -P` + `pwd -P` resolves a DIRECTORY link with builtins: one fork per
# link, zero exec. realpath/readlink -f would add an exec to every tool call
# and are not guaranteed on a minimal image. The one exception is the
# instruction-file link (~/.claude/CLAUDE.md), which links to a FILE that
# `cd -P` cannot resolve: it costs one plain `readlink` exec per hop, and a
# missing `readlink` blocks. That trade is recorded in ADR 0002.
#
# Three outcomes, none of them a silent allow: the node is absent (nothing is
# installed on this host — only the config names below are protected, which is
# what lets the matrix run in CI); it resolves (full root list); or it exists
# and does not resolve (block, because the protected set cannot be established).
# One exception: a config dir itself that does not resolve still lets a Read
# through, since Read cannot write a config name (see below).
#
# $cp_home is NOT a prefix root. It was, and that was wrong: ~/.claude holds
# this agent's own working state as well as its config — projects/*/memory,
# plans/, todos/, logs/ — and those are written with ordinary tools. Protecting
# the whole tree blocked every one of those writes, which broke agent memory
# and plan mode outright. What deserves protection is the config: the files
# that change what a session EXECUTES. See
# docs/adr/0002-anchor-the-guards-to-the-live-control-plane.md.
cp_roots=()
cp_files=()
cp_exact=()

# Config locations under $cp_home, as one list that every spelling in this file
# is derived from. bootstrap.sh symlinks the first seven out of the checkout
# (cp_linked; their realpaths are resolved below) from its own copy of these
# names: a dir added there must be added here by hand, and no test compares
# the two lists; plugins/ is Claude Code's
# own, listed by name whether or not it exists on this host, because plugin
# code executes. guard-state/ is written by hooks only, never by a session's
# tools: it holds the list of PRs a session created, which decides what
# guard-mcp passes (docs/adr/0028-guard-mcp-passes-set-monitor-only-for-prs-the-session-opened.md).
# A session that could write it could pass set_monitor for any PR.
cp_linked=( hooks rules bin commands agents skills output-styles )
cp_dnames=( "${cp_linked[@]}" plugins guard-state )
cp_fnames=( settings.json settings.local.json CLAUDE.md )

# Each name is protected as spelt under $cp_home and by its physical path.
# When the config dir, or a directory above it, is a link (a dotfiles checkout,
# or a CLAUDE_CONFIG_DIR that names one), a call spelt through the link's target
# misses every lexical entry, and a linked name such as <target>/hooks/x misses
# the link's realpath root too, though the kernel follows the link into it.
# See docs/adr/0029-guard-paths-protects-the-config-dir-by-its-physical-path.md.
#
# A config dir that exists and does not resolve blocks every call but a Read,
# as a config link does for writes: Read never writes, so it cannot reach a
# config name, and it is what a session needs to see why the dir does not
# resolve. Only an exact Read is exempt; an empty or unknown tool still blocks.
cp_bases=( "$cp_home" )
if [[ -e "$cp_home" || -L "$cp_home" ]]; then
  if cp_hphys=$(cd -P "$cp_home" 2>/dev/null && pwd -P); then
    [[ "$cp_hphys" != "$cp_home" ]] && cp_bases+=( "$cp_hphys" )
  elif [[ "$tool" != Read ]]; then
    echo "guard-paths: $cp_home exists but does not resolve; cannot establish the config dir's physical path. Blocking." >&2
    exit 2
  fi
fi
for cp_b in "${cp_bases[@]}"; do
  # The config dir node ITSELF, in each spelling, matched by equality only —
  # never as a prefix, and deliberately not substring-scanned, or every path
  # beneath it (including the agent's own state) would match on the parent
  # string alone.
  cp_exact+=( "$cp_b" )
  for cp_d in "${cp_dnames[@]}"; do
    cp_roots+=( "${cp_b%/}/$cp_d" )
  done
  for cp_f in "${cp_fnames[@]}"; do
    cp_files+=( "${cp_b%/}/$cp_f" )
  done
done

# Every link's realpath, not only the hooks link's parent (ADR 0002): the
# mechanism links resolve into the base snapshot and the rules link into the
# instance. Each resolved link's parent is a root whose exact files bootstrap
# reads. A link that exists and does not resolve blocks. The list is every
# directory bootstrap.sh links, skills and output-styles included.
cp_repos=()
for cp_d in "${cp_linked[@]}"; do
  cp_node="$cp_home/$cp_d"
  [[ -e "$cp_node" || -L "$cp_node" ]] || continue
  if cp_real=$(cd -P "$cp_node" 2>/dev/null && pwd -P); then
    cp_roots+=( "$cp_real" )
    # Once per tree, not once per link: today every link resolves into the
    # same checkout, and the per-repo list below would be added seven times.
    cp_seen=0
    for cp_r in ${cp_repos[@]+"${cp_repos[@]}"}; do
      [[ "$cp_r" == "${cp_real%/*}" ]] && cp_seen=1
    done
    (( cp_seen )) || cp_repos+=( "${cp_real%/*}" )
  else
    echo "guard-paths: $cp_node exists but does not resolve; cannot establish the protected roots. Blocking." >&2
    exit 2
  fi
done
for cp_repo in ${cp_repos[@]+"${cp_repos[@]}"}; do
  # Exact files, not a prefix on $cp_repo: a prefix would swallow
  # $cp_repo/.claude/worktrees/*, where control-plane pull requests are authored.
  # trimtab/ is executed by live hooks (`python3 -m trimtab.adapters.*`) and by
  # bin/trimtab; systemd/ holds the units bootstrap links into the user unit
  # directory, which run from the weekly timer outside every session guard.
  cp_roots+=( "$cp_repo/hooks" "$cp_repo/rules" "$cp_repo/bin" \
              "$cp_repo/trimtab" "$cp_repo/systemd" )
  cp_files+=( "$cp_repo/CLAUDE.md" "$cp_repo/instance.json" \
              "$cp_repo/settings.base.json" "$cp_repo/settings.instance.json" \
              "$cp_repo/machine.json" "$cp_repo/machine.example.json" \
              "$cp_repo/bootstrap.sh" "$cp_repo/guards/secrets.patterns" )
done

# The instruction-file link is a per-link root of its own: its target is
# protected even when no other link resolves into the same tree. `cd -P`
# cannot resolve a link to a FILE, so this is the one place the guard execs
# `readlink` (plain, not `-f`, which older macOS lacks), and only when the
# node is a link. A missing `readlink`, a loop, or a target that is not a file
# means the protected set cannot be established: block (ADR 0002).
cp_node="$cp_home/CLAUDE.md"
if [[ -L "$cp_node" ]]; then
  cp_link="$cp_node"
  cp_hops=0
  cp_real=""
  # A relative target is joined to the link's directory as text; the one
  # `cd -P` below resolves the result physically, `..` included.
  while [[ -L "$cp_link" ]]; do
    (( ++cp_hops > 40 )) && break
    cp_tgt=$(readlink -- "$cp_link" 2>/dev/null) || break
    [[ "$cp_tgt" == /* ]] || cp_tgt="${cp_link%/*}/$cp_tgt"
    cp_link="$cp_tgt"
  done
  # A target directly under / leaves an empty directory part; `cd -P ""` fails
  # on some bash builds and is a no-op (the cwd) on others, so name / outright.
  cp_dir="${cp_link%/*}"
  if [[ ! -L "$cp_link" && -f "$cp_link" ]] \
     && cp_dir=$(cd -P "${cp_dir:-/}" 2>/dev/null && pwd -P); then
    cp_real="${cp_dir%/}/${cp_link##*/}"
  fi
  if [[ -z "$cp_real" ]]; then
    echo "guard-paths: $cp_node exists but does not resolve to a file; cannot establish the protected roots. Blocking." >&2
    exit 2
  fi
  cp_files+=( "$cp_real" )
fi

cp_deny() {
  cat >&2 <<EOF
BLOCKED by guard-paths: $1
Target: $2

This is the LIVE control plane — the hooks, rules, settings and bootstrap that
the running session actually loads. A change here takes effect in the next
session with no commit, no pull request and no CI.

Reading is not blocked. Use the Read, Grep and Glob tools on these files freely.

To CHANGE one, work in a git worktree and open a pull request; a worktree copy
is deliberately not protected, so the review path stays open. Do not set
HOOK_ALLOW_PATHS yourself — that is the operator's decision.
EOF
  exit 2
}

# Normalise to an absolute, lexically-resolved path. A Bash token carries `~`,
# $HOME, $CLAUDE_CONFIG_DIR and $XDG_CONFIG_HOME (the unit directory's spelling)
# unexpanded, so expand them here; resolve a relative path against
# the hook's cwd; collapse `..` lexically.
cp_n=""
cp_norm() {
  local p="$1" part reset_f=1
  local -a out=()
  case "$p" in
    '~')                                          p="${HOME:-}" ;;
    '~/'*)                                        p="${HOME:-}/${p#\~/}" ;;
    '$HOME'|'${HOME}')                            p="${HOME:-}" ;;
    '$HOME/'*)                                    p="${HOME:-}/${p#\$HOME/}" ;;
    '${HOME}/'*)                                  p="${HOME:-}/${p#\$\{HOME\}/}" ;;
    '$CLAUDE_CONFIG_DIR'|'${CLAUDE_CONFIG_DIR}')  p="$cp_home" ;;
    '$CLAUDE_CONFIG_DIR/'*)                       p="$cp_home/${p#\$CLAUDE_CONFIG_DIR/}" ;;
    '${CLAUDE_CONFIG_DIR}/'*)                     p="$cp_home/${p#\$\{CLAUDE_CONFIG_DIR\}/}" ;;
    '$XDG_CONFIG_HOME'|'${XDG_CONFIG_HOME}')      p="${XDG_CONFIG_HOME:-}" ;;
    '$XDG_CONFIG_HOME/'*)                         p="${XDG_CONFIG_HOME:-}/${p#\$XDG_CONFIG_HOME/}" ;;
    '${XDG_CONFIG_HOME}/'*)                       p="${XDG_CONFIG_HOME:-}/${p#\$\{XDG_CONFIG_HOME\}/}" ;;
  esac
  [[ "$p" == /* ]] || p="$PWD/$p"

  # Splitting on / is an unquoted expansion, so globbing must be off or a path
  # containing * would expand against the filesystem. Restore the previous
  # state: the token loop below relies on globbing being on.
  [[ -o noglob ]] && reset_f=0
  set -f
  local IFS=/
  for part in $p; do
    case "$part" in
      ''|.) ;;
      ..)   (( ${#out[@]} )) && out=( "${out[@]:0:${#out[@]}-1}" ) ;;
      *)    out+=( "$part" ) ;;
    esac
  done
  (( reset_f )) && set +f
  cp_n="/${out[*]}"
}

# guard-secrets reads the private pattern file named here. Protect it by its
# normalised path, or a value spelt with `.` or `..` would never equal the
# normalised path an Edit names, and would go unprotected. A value that is not
# absolute blocks every call: guard-secrets opens it verbatim against its cwd,
# so a relative value, or a literal `~` (settings env is not shell-expanded),
# names a different file whenever the session changes directory, and there is
# no fixed file to protect. Resolving it against this call's $PWD only moved
# the gap (an empty copy elsewhere, then `cd`), and the command scan's own `~`
# expansion never matched a literal `~` directory at all.
if [[ -n "${TRIMTAB_SECRET_PATTERNS:-}" ]]; then
  cp_pat="$TRIMTAB_SECRET_PATTERNS"
  if [[ "$cp_pat" != /* ]]; then
    echo "guard-paths: TRIMTAB_SECRET_PATTERNS is not an absolute path ($cp_pat); guard-secrets reads it against the session's cwd, so the file cannot be protected. Set it to an absolute path. Blocking." >&2
    exit 2
  fi
  cp_norm "$cp_pat"
  cp_files+=( "$cp_n" )
  # ...and physically, as guard-secrets opens it: through a symlinked directory,
  # or `..` after one (resolved by the kernel, not as text), the spelt path
  # names another file. The directory part is resolved as spelt with `cd -P`;
  # if it does not resolve there is no file to protect (and guard-secrets could
  # not read one), so block.
  cp_pdir="${cp_pat%/*}"
  if cp_pphys=$(cd -P "${cp_pdir:-/}" 2>/dev/null && pwd -P); then
    cp_norm "${cp_pphys%/}/${cp_pat##*/}"
    cp_files+=( "$cp_n" )
  else
    echo "guard-paths: the directory of TRIMTAB_SECRET_PATTERNS ($cp_pat) does not resolve; cannot establish the file's physical path. Blocking." >&2
    exit 2
  fi
fi

# The base snapshot store (snapshot installs, ADR 0003): always a protected
# prefix, whether or not it exists yet, so the rollback snapshot is covered.
# Normalised for the same reason as the pattern file above. A relative
# XDG_DATA_HOME is invalid under the XDG base directory spec and is ignored,
# so ~/.local/share applies, as it does for any conforming installer. A
# relative HOME is no better a base and is ignored the same way (no store
# root, as with HOME unset): the walk below must only ever see an absolute
# path, or stripping components never reaches the end and the hook hangs.
cp_data="${XDG_DATA_HOME:-}"
[[ "$cp_data" == /* ]] || cp_data=""
[[ -z "$cp_data" && -n "${HOME:-}" ]] && cp_data="${HOME%/}/.local/share"
[[ "$cp_data" == /* ]] || cp_data=""
# Protected twice: as spelt (normalised), and physically. When the data
# directory or one above it is a symlink, an Edit can name the snapshot through
# the link's target, which the lexical root never matches. The store may not
# exist yet, so the deepest existing ancestor is resolved and the rest
# appended.
#
# The walk runs on the path AS SPELT, not the normalised one: `..` after a
# symlinked directory means the link target's parent, which is where an
# installer's mkdir -p puts the store, and collapsing it as text first would
# protect the wrong tree. Only the part that does not exist yet is appended
# as text, and only when it holds no `..`: after a component that does not
# exist, the kernel cannot resolve a `..`, and collapsing it as text could name
# a different tree from the one mkdir -p creates, through any symlink after it.
# That spelling, and a link on the way that does not resolve to a directory
# (dangling, a loop, or a link to a file), block, as a config link does above.
#
# The lexical root stays even when `..` follows a symlinked directory and it
# therefore names a tree that is not the store: an Edit spelt exactly as
# XDG_DATA_HOME is normalised to that same tree, while the kernel writes into
# the real store, so dropping the root would let that one spelling through.
# Keeping it over-blocks the unrelated tree, the safe direction. It covers
# only that spelling: cp_norm collapses `..` as text in every Edit path, so
# `<any symlink>/../<protected name>` still misses every root, here and for
# the other roots. That gap is not closed here; ADR 0002 records it as a
# known gap.
if [[ -n "$cp_data" ]]; then
  cp_norm "$cp_data/trimtab/core"
  cp_store="$cp_n"
  cp_roots+=( "$cp_store" )
  cp_anc="${cp_data%/}/trimtab/core"
  cp_rest=""
  # The `*/*` test is a backstop: a component with no `/` left would strip to
  # itself forever. cp_anc is absolute, so it never fires in practice; if it
  # did, the `cd -P` below fails on a missing relative path and blocks.
  while [[ -n "$cp_anc" && "$cp_anc" == */* && ! -d "$cp_anc" ]]; do
    if [[ -L "$cp_anc" ]]; then
      if [[ -e "$cp_anc" ]]; then
        echo "guard-paths: $cp_anc is a link to something that is not a directory; the snapshot store cannot live under it. Blocking." >&2
      else
        echo "guard-paths: $cp_anc is a link that does not resolve (dangling, or a loop); cannot establish the snapshot store's physical path. Blocking." >&2
      fi
      exit 2
    fi
    cp_rest="/${cp_anc##*/}$cp_rest"
    cp_anc="${cp_anc%/*}"
  done
  if [[ "$cp_rest/" == */../* ]]; then
    echo "guard-paths: the snapshot store path ($cp_data/trimtab/core) has '..' after a component that does not exist; cannot establish its physical path. Blocking." >&2
    exit 2
  fi
  if cp_phys=$(cd -P "${cp_anc:-/}" 2>/dev/null && pwd -P); then
    cp_norm "${cp_phys%/}$cp_rest"
    [[ "$cp_n" != "$cp_store" ]] && cp_roots+=( "$cp_n" )
  else
    echo "guard-paths: ${cp_anc:-/} exists but does not resolve; cannot establish the snapshot store's physical path. Blocking." >&2
    exit 2
  fi
fi

# The user systemd unit directory: a protected PREFIX, so unit files, the links
# bootstrap makes, `<unit>.d/` drop-ins and `*.wants/` links are all covered. An
# edit to ExecStart= runs any command from the weekly timer, outside every
# session guard, with no pull request (ADR 0002's case). bootstrap writes to
# ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user; both spellings are protected,
# whichever one is in force, so the set only widens. As for XDG_DATA_HOME
# above, a value that is not absolute is invalid under the XDG spec and is
# ignored, and so is a relative HOME. When the directory exists it is protected
# physically too (a dotfiles link); one that exists and does not resolve
# blocks, as a config link does. Routes that write here without naming the
# directory (`systemctl --user edit`, `enable`) are outside a path guard, and
# no other guard closes them yet.
cp_units=()
[[ "${HOME:-}" == /* ]] && cp_units+=( "${HOME%/}/.config/systemd/user" )
[[ "${XDG_CONFIG_HOME:-}" == /* ]] && cp_units+=( "${XDG_CONFIG_HOME%/}/systemd/user" )
for cp_u in ${cp_units[@]+"${cp_units[@]}"}; do
  cp_norm "$cp_u"
  cp_roots+=( "$cp_n" )
  [[ -e "$cp_u" || -L "$cp_u" ]] || continue
  if cp_uphys=$(cd -P "$cp_u" 2>/dev/null && pwd -P); then
    [[ "$cp_uphys" != "$cp_n" ]] && cp_roots+=( "$cp_uphys" )
  else
    echo "guard-paths: $cp_u exists but does not resolve; cannot establish the systemd unit directory's physical path. Blocking." >&2
    exit 2
  fi
done

cp_hit() {   # $1 = a normalised absolute path
  local r
  for r in "${cp_roots[@]}"; do
    [[ "$1" == "$r" || "$1" == "$r"/* ]] && return 0
  done
  # cp_exact is matched here but never substring-scanned — see where it is set.
  for r in "${cp_files[@]}" "${cp_exact[@]}"; do
    [[ "$1" == "$r" ]] && return 0
  done
  return 1
}

cp_check() {
  [[ -z "$1" ]] && return 0
  cp_norm "$1"
  cp_hit "$cp_n" && cp_deny "$2" "$1"
  return 0
}

# Whole-string scan, in addition to the token loop. The token loop cannot see
# inside `python3 -c '...'`, a heredoc, or a no-space redirect — but each of
# those must still contain the literal path, so match the string as well.
#
# Any reference is blocked, read-only commands included. Classifying a command
# as "mutating" is semantic detection, which must never be the sole defence,
# and the verb surface (sed -i, tee, >, cp, patch, find -exec,
# python3 -c, ...) is open-ended. A path set is enumerable; a verb set is not.
cp_scan_cmd() {
  local c="$1" r frag reset_f=1
  c="${c//\$\{HOME\}/${HOME:-}}";             c="${c//\$HOME/${HOME:-}}"
  c="${c//\$\{CLAUDE_CONFIG_DIR\}/$cp_home}"; c="${c//\$CLAUDE_CONFIG_DIR/$cp_home}"
  c="${c//\$\{XDG_CONFIG_HOME\}/${XDG_CONFIG_HOME:-}}"; c="${c//\$XDG_CONFIG_HOME/${XDG_CONFIG_HOME:-}}"
  c="${c//\~\//${HOME:-}/}"

  # An absolute root matches as a substring however it is quoted or glued.
  for r in "${cp_roots[@]}" "${cp_files[@]}"; do
    [[ "$c" == *"$r"* ]] && cp_deny "shell command referencing the live control plane" "$1"
  done

  # A RELATIVE reference cannot be matched as a substring — it has to be
  # resolved against the hook's cwd, which only cp_norm does. The token loop
  # resolves the tokens it can see, but a path welded to quotes, parens, a
  # redirect or a --flag= never becomes a token: `python3 -c
  # "open('hooks/guard-paths.sh','w')"` from the live repo wrote to the guard
  # while every absolute spelling of it was blocked. Split on the punctuation
  # that delimits a path in shell and in code, and resolve each fragment.
  #
  # This also covers the env-names.sh exemption, which exits 0 without ever
  # reaching the token loop — leaving this scan as its only check.
  [[ -o noglob ]] && reset_f=0
  set -f
  local IFS=$' \t\n\047\042(),;|&<>={}:'
  for frag in $c; do
    # Same bare-word rule as the token loop: a fragment with no / or . is a
    # word like `hooks`, and blocking those would block the English word. A
    # glob word (`CLAUDE*`) is not a bare word: it names whatever it expands to.
    [[ "$frag" == *[/.]* || "$frag" == *[\*\?\[]* ]] || continue
    cp_check "$frag" "shell command referencing the live control plane"
    # The harness layer is defined further down; it runs here too, so a path
    # welded into `python3 -c "open('.claude/settings.json','w')"` is seen.
    # cp_check has just normalised the fragment into cp_n; reuse it.
    hl_check "$frag" 0 "shell command referencing" "$cp_n"
  done
  (( reset_f )) && set +f
  return 0
}

# ---- policy ------------------------------------------------------------
# Directory patterns. Anchored with */ so they match at any depth in an
# absolute path, and bare so they also match a repo-relative path.
deny_dirs=(
  '*/.git/*'          '.git/*'
  '*/secrets/*'       'secrets/*'
  '*/.ssh/*'          '*/.gnupg/*'
  '*/.config/gcloud/*'
)

# Filename patterns, matched against the basename at any depth.
# Deliberately absent: production.py, prod.yml, .npmrc — ordinary config
# files in a FastAPI/GCP repo; blocking them was daily noise. A secret
# *inside* one of them is still caught by guard-secrets' content rules.
deny_names=(
  '.env' '.env.*'
  '*.pem' '*.key' '*.p12' '*.pfx' '*.jks'
  'id_rsa' 'id_ed25519' 'id_ecdsa'
  'service-account*.json' '*credentials*.json'
  '.netrc' '.pgpass'
)

# Filenames that look protected but are safe: templates and docs.
allow_names=(
  '.env.example' '.env.sample' '.env.template'
  '*.example' '*.sample' '*.template' '*.md'
  '*_sample.*' '*-sample.*' '*_example.*' '*-example.*'
)

check_path() {
  local p="$1" why="$2" d n a
  [[ -z "$p" ]] && return 0

  local base="${p##*/}"

  shopt -s nocasematch
  for a in "${allow_names[@]}"; do
    if [[ "$base" == $a ]]; then shopt -u nocasematch; return 0; fi
  done
  for d in "${deny_dirs[@]}"; do
    if [[ "$p" == $d ]]; then shopt -u nocasematch; deny "$why (protected directory)" "$p"; fi
  done
  for n in "${deny_names[@]}"; do
    if [[ "$base" == $n ]]; then shopt -u nocasematch; deny "$why (protected filename)" "$p"; fi
  done
  shopt -u nocasematch
  return 0
}

# The control-plane check runs BEFORE check_path, as its own list. It has to:
# check_path consults allow_names first, and that list matches *.md, *.example,
# *.template and *.sample — so a rules/*.md file and machine.example.json would
# be waved through before any deny list was reached. Keeping it a separate
# function also means no future addition to allow_names can reach it.
#
# Enforcement is an allow-list of exactly one tool rather than a deny-list of
# five: an empty, unknown or future tool_name enforces by default. That holds
# only for a tool the matcher sends here and whose path key parse() reads.
# NotebookEdit had neither until ADR 0030, which is why the registration test
# in the matrix pins the matcher. See
# docs/adr/0030-guard-paths-is-registered-for-and-reads-notebookedit.md.
case "$tool" in
  Read) ;;
  *)    cp_check "$file" "write to the live control plane" ;;
esac

check_path "$file" "file access"

# ---- harness and write allowlist ---------------------------------------
# Two layers, both inside a git repository (the nearest ancestor holding a
# .git file or directory, so a worktree is its own repository):
#
# 1. Always refused, in every repository and for every tool but Read: the
#    repository's harness and controls. .claude/ (settings, hooks, the
#    project's rules directory, agents, commands, skills, the Trimtab lock,
#    the write allowlist itself) except .claude/worktrees/; CLAUDE.md and
#    CLAUDE.local.md at any depth; .github/; CODEOWNERS (root and docs/);
#    the scanner exemption lists; and the authorised plans. A Bash command
#    naming one of these paths is refused the same way, reads included: as
#    with the control plane, a path set is enumerable and a verb set is not.
# 2. Default-deny, only where the repository has .claude/write-allowlist:
#    an Edit, Write, MultiEdit or NotebookEdit outside the listed globs is
#    refused. One glob per line, relative to the repository root, `#`
#    comments and blank lines ignored, `**` read as `*` (which already spans
#    directories here). The list cannot widen layer 1. Bash is not confined
#    by it: any program can write anywhere, so shell writes outside the list
#    are left to CODEOWNERS at merge.
#
# The operator lifts both layers for a session that develops the harness by
# exporting HOOK_ALLOW_HARNESS=1 before launching claude. The other checks in
# this guard stay on. See
# docs/adr/0039-guard-paths-refuses-the-harness-and-enforces-a-write-allowlist.md.
hl_on=1
[[ "${HOOK_ALLOW_HARNESS:-0}" == "1" ]] && hl_on=0

# Spelt in two pieces so the base's citation test, which refuses any plans
# directory path in a tracked file, does not read this as a citation.
hl_plans="docs/""plans/authorised"

hl_root=""
hl_find_root() {   # $1 = a normalised absolute path; sets hl_root or ""
  local d="$1"
  hl_root=""
  while [[ -n "$d" ]]; do
    if [[ -e "$d/.git" ]]; then hl_root="$d"; return 0; fi
    d="${d%/*}"
  done
  return 1
}

hl_refused() {   # $1 = a path relative to its repository root
  # Compared lower-cased: on a case-insensitive filesystem (macOS by default)
  # `Claude.md` or `.GitHub/` names the same file as the protected spelling.
  local rel="${1,,}" base plans="${hl_plans,,}"
  base="${rel##*/}"
  case "$rel" in
    .claude/worktrees|.claude/worktrees/*) return 1 ;;
    .claude|.claude/*|.github|.github/*) return 0 ;;
    codeowners|docs/codeowners) return 0 ;;
    .gitleaks.toml|.trivyignore|.bandit-allowlist.yml) return 0 ;;
  esac
  [[ "$rel" == "$plans" || "$rel" == "$plans"/* ]] && return 0
  [[ "$base" == claude.md || "$base" == claude.local.md ]] && return 0
  return 1
}

# A Bash word holding glob characters names whatever the shell expands it to,
# which the literal checks above never see (`.c*/hooks/x`, `CLAUDE*`).
# Expand it the way the shell would, with bash's own pathname expansion (no
# exec), and judge every match. nocaseglob is always on, since a
# case-insensitive filesystem matches either way; dotglob is on when the
# command line mentions it or GLOBIGNORE, either of which lets `*` reach
# dotfiles. A glob that matches nothing stays a literal word in the shell, so
# it names no protected file. Variables built at run time stay out of reach
# of any path guard; ADR 0039 records that residual.
hl_dotglob=0
[[ "$cmd" == *dotglob* || "$cmd" == *GLOBIGNORE* ]] && hl_dotglob=1
# Expansion runs in this process, and the hook's registered timeout (10 s in
# settings.base.json) is not a safe backstop: a hook that is killed does not
# exit 2, and Claude Code treats that as non-blocking, so a slow expansion
# would let the command through. The cost is therefore bounded before it
# starts: a word with more than hl_glob_max_parts wildcard path components
# (`/*/*/*`) is refused as too broad to check, and more than hl_glob_max_hits
# matches are refused rather than judged one by one. Both fail closed; a real
# command rarely needs either.
hl_glob_max_parts=2
hl_glob_max_hits=1000
hl_glob_hits() {   # $1 = an absolute path containing * ? or [
                   # 0 = a match is refused, 1 = none is, 2 = too broad to check
  local -a hits=() parts=()
  local m n=0 restore_f=0 restore_dot=0 restore_case=0 restore_null=0 IFS=/
  [[ -o noglob ]] && restore_f=1
  set -f
  # shellcheck disable=SC2206
  parts=( $1 )
  for m in ${parts[@]+"${parts[@]}"}; do
    [[ "$m" == *[\*\?\[]* ]] && n=$((n + 1))
  done
  if (( n > hl_glob_max_parts )); then
    (( restore_f )) || set +f
    return 2
  fi
  IFS=
  shopt -q dotglob || restore_dot=1
  shopt -q nocaseglob || restore_case=1
  shopt -q nullglob || restore_null=1
  set +f
  shopt -s nocaseglob nullglob
  (( hl_dotglob )) && shopt -s dotglob
  # shellcheck disable=SC2206
  hits=( $1 )
  (( restore_f )) && set -f
  (( restore_dot )) && shopt -u dotglob
  (( restore_case )) && shopt -u nocaseglob
  (( restore_null )) && shopt -u nullglob
  (( ${#hits[@]} > hl_glob_max_hits )) && return 2
  for m in ${hits[@]+"${hits[@]}"}; do
    hl_find_root "$m" || continue
    [[ "$m" == "$hl_root" ]] && continue
    hl_refused "${m#"$hl_root"/}" && return 0
  done
  return 1
}

hl_deny() {
  cat >&2 <<EOF
BLOCKED by guard-paths: $1
Target: $2

This path is part of the repository's harness or controls (or outside its
write allowlist). Plan approval never covers these, and neither does a task
that merely involves them.

If the change is genuinely needed, stop and ask the operator, naming the file.
The operator lifts this check for a harness-development session by exporting
HOOK_ALLOW_HARNESS=1 before launching claude. Do not set it yourself, and do
not look for another route.
EOF
  exit 2
}

hl_allowed() {   # $1 = repository root, $2 = relative path; 0 = listed
  local list="$1/.claude/write-allowlist" line pat
  if [[ ! -r "$list" ]]; then
    hl_deny "write allowlist exists but cannot be read (fail closed)" "$list"
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"   # trailing space and a CRLF's \r
    [[ -z "$line" ]] && continue
    pat="${line//\*\*/*}"
    pat="${pat#/}"
    pat="${pat#./}"
    # A trailing slash means "this directory": everything under it.
    [[ "$pat" == */ ]] && pat="${pat}*"
    # shellcheck disable=SC2053
    [[ "$2" == $pat ]] && return 0
  done < "$list"
  return 1
}

hl_check_abs() {   # $1 = absolute path, $2 = write tool 1/0, $3 = reason, $4 = as given
  local abs="$1" rel
  # A glob is judged by what it expands to, wherever the glob sits (above the
  # repository root too, where the literal path finds no root).
  if (( ! $2 )) && [[ "$abs" == *[\*\?\[]* ]]; then
    local g
    hl_glob_hits "$abs"
    g=$?
    (( g == 0 )) && hl_deny "$3 (a glob that expands to a repository harness or control path)" "$4"
    (( g == 2 )) && hl_deny "$3 (a glob too broad to check; narrow it)" "$4"
  fi
  hl_find_root "$abs" || return 0
  [[ "$abs" == "$hl_root" ]] && return 0
  rel="${abs#"$hl_root"/}"
  hl_refused "$rel" && hl_deny "$3 (repository harness or control)" "$4"
  # -L as well as -e: a dangling link at the allowlist's path must not read
  # as "no allowlist" and switch default-deny off; hl_allowed then finds it
  # unreadable and refuses (fail closed).
  if (( $2 )) && [[ -e "$hl_root/.claude/write-allowlist" || -L "$hl_root/.claude/write-allowlist" ]]; then
    hl_allowed "$hl_root" "$rel" || hl_deny "write outside the repository's write allowlist" "$4"
  fi
  return 0
}

hl_check() {   # $1 = path as given, $2 = 1 for a write tool, $3 = reason,
               # $4 = its normalised form when the caller already has it
  (( hl_on )) || return 0
  [[ -z "$1" ]] && return 0
  local spelt anc rest="" phys
  if [[ -n "${4:-}" ]]; then spelt="$4"; else cp_norm "$1"; spelt="$cp_n"; fi
  hl_check_abs "$spelt" "$2" "$3" "$1"
  (( $2 )) || return 0
  # A target that is itself a symlink writes wherever the link points, which
  # neither the spelt path nor the directory walk below sees. Writing through
  # a link is rare; refusing it closes the route.
  if [[ -L "$spelt" ]]; then
    hl_deny "$3 (the target is a symlink; write the real file instead)" "$1"
  fi
  # A hard link is invisible to every path check: an Edit of `src/s.json`
  # rewrites `.claude/settings.json` if the two share an inode. bash has no
  # builtin for the link count, so this costs one `stat` exec per write to an
  # existing file (GNU spelling first, then BSD); if neither answers, block.
  if [[ -f "$spelt" ]]; then
    local nl
    nl=$(stat -c %h -- "$spelt" 2>/dev/null) \
      || nl=$(stat -f %l -- "$spelt" 2>/dev/null) \
      || hl_deny "$3 (cannot read the target's link count; fail closed)" "$1"
    [[ "$nl" =~ ^[0-9]+$ ]] || hl_deny "$3 (cannot read the target's link count; fail closed)" "$1"
    (( nl > 1 )) && hl_deny "$3 (the target has other hard links, one of which may be a protected file)" "$1"
  fi
  # A write lands where the kernel resolves the path, which a symlink inside an
  # allowlisted directory can point into .claude/ or another refused path.
  # Resolve the deepest existing ancestor physically (one `cd -P`, as for the
  # snapshot store above) and judge that path too. One that does not resolve
  # blocks.
  anc="$spelt"
  while [[ -n "$anc" && ! -d "$anc" ]]; do
    # A link on the way that does not resolve to a directory (dangling, a
    # loop, or a link to a file above the target) hides where the write would
    # go, or where a later mkdir would put it: block.
    if [[ -L "$anc" && "$anc" != "$spelt" ]]; then
      hl_deny "$3 (a symlink on the path that does not resolve to a directory)" "$1"
    fi
    rest="/${anc##*/}$rest"
    anc="${anc%/*}"
  done
  if ! phys=$(cd -P "${anc:-/}" 2>/dev/null && pwd -P); then
    hl_deny "$3 (cannot resolve the target's physical path; fail closed)" "$1"
  fi
  phys="${phys%/}$rest"
  [[ "$phys" != "$spelt" ]] && hl_check_abs "$phys" "$2" "$3" "$1"
  return 0
}

case "$tool" in
  Read) ;;
  *)    hl_check "$file" 1 "write to" ;;
esac

# ---- bash commands -----------------------------------------------------
# Don't basename a whole command line — that only ever inspects the last
# token. Pull out every path-shaped argument and check each one.
if [[ -n "$cmd" ]]; then
  # The redacting inspector is the sanctioned way to answer "what config
  # exists" without exposing values, so its own invocation must not be
  # blocked by the path scan below. Only exempt a BARE invocation: no shell
  # metacharacters, so this cannot be used as a bypass by chaining
  # (`env-names.sh x && cat .env`) or substitution.
  if [[ "$cmd" == *env-names.sh* ]]; then
    if [[ "$cmd" =~ [\;\|\&\>\<\`] || "$cmd" == *'$('* ]]; then
      deny "shell metacharacters alongside env-names.sh (possible bypass)" "$cmd"
    fi
    first="${cmd%% *}"
    if [[ "${first##*/}" == "env-names.sh" ]]; then
      # Exempt the invocation, not its arguments: the inspector must not become
      # a reading channel into the control plane.
      cp_scan_cmd "${cmd#"$first"}"
      exit 0
    fi
    # Mentioned but not invoked bare (e.g. `git add bin/env-names.sh`,
    # `chmod +x bin/env-names.sh`): not an exemption, but not a denial
    # either — fall through to the normal token scan, which still blocks
    # any protected path on the line.
  fi

  cp_scan_cmd "$cmd"

  # shellcheck disable=SC2086
  for tok in $cmd; do
    tok="${tok%[,;\"\')]}"
    tok="${tok#[\"\'(]}"
    [[ "$tok" == -* ]] && continue
    [[ "$tok" == *[/.]* || "$tok" == *[\*\?\[]* ]] || continue
    # Relative tokens resolve against the hook's cwd, which the whole-string
    # scan above cannot do.
    cp_check "$tok" "shell command referencing"
    tok_n="$cp_n"
    check_path "$tok" "shell command referencing"
    hl_check "$tok" 0 "shell command referencing" "$tok_n"
  done
fi

exit 0
