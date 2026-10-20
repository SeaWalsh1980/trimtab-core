#!/usr/bin/env bash
# PreToolUse hook, matcher: Bash
#
# Blocks destructive infrastructure commands. Exit 2 = blocked, stderr is fed
# back to Claude as the reason. Exit 0 = allowed.
#
# Fails CLOSED: a payload that cannot be parsed blocks rather than allowing.
# Claude Code treats every exit code except 2 as non-blocking, so a guard
# that errors out (missing jq, bad JSON, empty stdin) looks identical to a guard
# that is allowing everything — unless it deliberately exits 2.
# See docs/adr/0001-guards-fail-closed-on-missing-dependencies.md.
#
# Override for a single command by prefixing it with:  ALLOW_DESTRUCTIVE=1
#   — except the push rules, which the prefix does not lift: the model can
#   type a prefix itself, so it is no override for a rule that must hold
#   (docs/adr/0036-guard-bash-refuses-force-pushes-and-pushes-to-the-default-branch.md).
# Override for a whole session:  export HOOK_ALLOW_DESTRUCTIVE=1
#   (must be exported before launching claude, or set in settings env —
#    an inline prefix on the Bash tool's command never reaches this process)
#
# KNOWN LIMITATION — matching is over the whole command string, including
# heredoc bodies, commit messages and --grep arguments: text that is data and
# will never execute. Anchoring the keywords to a command-word start (below)
# removes the accidental hits, but a command that legitimately *quotes*
# `rm -rf` in documentation or test data still blocks. Telling code from data
# properly means parsing shell, which is not worth it here. Prefer writing such
# content with a file-writing tool over a heredoc — this hook sees Bash
# commands only, so Write/Edit are unaffected.

set -uo pipefail   # deliberately not -e: an unexpected non-zero must not
                   # short-circuit into a silent allow

payload=$(cat)

# An empty payload is not an empty command. jq reports empty stdin as a
# successful parse of nothing, which the empty-command allow below then waves
# through; only the python3 branch raised. Truncated stdin and a missing `cat`
# both land here. Everything after the parse is a bash builtin, except that
# the push rules' tokeniser runs python3 (or jq) again for a `bash -c` code
# string, so jq/python3 and cat are still this guard's whole external surface.
if [[ -z "$payload" ]]; then
  echo "guard-bash: empty hook payload. Blocking (fail closed)." >&2
  exit 2
fi

# The push rules' quote-aware tokeniser, defined here so the payload parse
# below can run it in the same interpreter. One regular-expression scan over
# a command (backslash-newlines joined), so its cost is linear. Each piece is
# one line, a tag and its text, newlines in quoted text flattened to spaces:
#   W  whitespace          C  a comment (`#` at the start of a word)
#   R  a redirection       O  an operator (a newline is printed as `;`)
#   Q  quoted text, quotes and escapes removed
#   L  unquoted literal text
# The same pattern is written for python3 and for jq; the guard matrix checks
# that the two agree. jq's scan is superlinear in the number of pieces, so
# past TOK_JQ_MAX characters it is not trusted to finish within the hook
# timeout: the parse leaves such a command untokenised, and the push rules
# then need python3 for it.
read -r -d '' TOK_PY <<'PY' || true
import re
_TOK = re.compile(
    r"(?P<W>[ \t]+)|(?P<C>(?<![^ \t\n;&|()`])#[^\n]*)|(?P<R>&>>?|[0-9]*[<>]+[&|]?)"
    r"|(?P<O>&&|\|\||\|&|;;|[;&|()`\n])|(?P<S>'[^']*'?)|(?P<D>\"(?:[^\"\\]|\\[\s\S])*\"?)"
    r"|(?P<B>\\[\s\S]?)|(?P<L>[^ \t\n;&|()`<>'\"\\]+)|(?P<A>[\s\S])")
_DQ = re.compile(r'(?:[^"\\]|\\[\s\S])*')
def tok(s):
    out = []
    for m in _TOK.finditer(s.replace("\\\n", " ")):
        k, v = m.lastgroup, m.group()
        if k == "S":
            v = v[1:]
            k, v = "Q", v[:-1] if v.endswith("'") else v
        elif k == "D":
            k, v = "Q", re.sub(r'\\([\\"$`])', r"\1", _DQ.match(v, 1).group())
        elif k == "B":
            k, v = "Q", v[1:]
        elif k == "O":
            v = ";" if v == "\n" else v
        elif k == "A":
            k = "L"
        out.append((k + v).replace("\n", " "))
    return out
PY
read -r -d '' TOK_JQ <<'JQ' || true
gsub("\\\\\n"; " ")
| [ match("(?<W>[ \\t]+)|(?<C>(?<![^ \\t\\n;&|()`])#[^\\n]*)|(?<R>&>>?|[0-9]*[<>]+[&|]?)|(?<O>&&|\\|\\||\\|&|;;|[;&|()`\\n])|(?<S>'[^']*'?)|(?<D>\"(?:[^\"\\\\]|\\\\[\\s\\S])*\"?)|(?<B>\\\\[\\s\\S]?)|(?<L>[^ \\t\\n;&|()`<>'\"\\\\]+)|(?<A>[\\s\\S])"; "g")
    | .captures | map(select(.string != null)) | .[0] ]
| .[]
| .name as $k | .string as $v
| if $k == "S" then "Q" + ($v[1:] | if endswith("'") then .[:-1] else . end)
  elif $k == "D" then "Q" + ($v[1:] | capture("^(?<c>(?:[^\"\\\\]|\\\\[\\s\\S])*)").c
                            | gsub("\\\\(?<x>[\\\\\"$`])"; .x))
  elif $k == "B" then "Q" + $v[1:]
  elif $k == "O" then "O" + (if $v == "\n" then ";" else $v end)
  elif $k == "A" then "L" + $v
  else $k + $v end
| gsub("\n"; " ")
JQ
TOK_JQ_MAX=32768

# Extract .cwd (first line, newlines flattened), the command's tokens (a count
# line, then that many piece lines; -1 when not tokenised) and
# .tool_input.command (the rest), using jq if present, else python3. Only a
# command that names push or git, once quotes are removed, is tokenised here.
# The cwd is the Bash tool's working directory, which a bare `git push`
# pushes from; the hook process's own $PWD is the fallback when the payload
# carries none.
# A parse failure must be distinguishable from an empty command: the former
# blocks, the latter is allowed. Trusting the output alone conflates them.
if command -v jq >/dev/null 2>&1; then
  if ! out=$(printf '%s' "$payload" | jq -r --argjson max "$TOK_JQ_MAX" "def tok: $TOK_JQ;"'
      ((.cwd // "") | tostring | gsub("\n"; " ")),
      ( (.tool_input.command // "") as $raw
        | ($raw | if type == "string" then . else tojson end) as $c
        | ( if ($c | length) <= $max and ($c | gsub("[\"'"'"'\\\\]"; "") | test("push|git"))
            then [ $c | tok ] | length, .[]
            else -1 end ),
          $raw )' 2>/dev/null); then
    echo "guard-bash: cannot parse hook payload. Blocking (fail closed)." >&2
    exit 2
  fi
elif command -v python3 >/dev/null 2>&1; then
  # -I: isolated, so a json.py in the working directory (the repository under
  # work) or a PYTHON* variable cannot stand in for the standard library.
  if ! out=$(printf '%s' "$payload" | python3 -I -c "$TOK_PY"'
import json, sys
d = json.load(sys.stdin)
print(str(d.get("cwd") or "").replace("\n", " "))
c = (d.get("tool_input") or {}).get("command") or ""
t = re.sub(r"[\"'"'"'\\]", "", c if isinstance(c, str) else "")
if isinstance(c, str) and ("push" in t or "git" in t):
    pieces = tok(c)
    print(len(pieces))
    for p in pieces:
        print(p)
else:
    print(-1)
print(c)' 2>/dev/null); then
    echo "guard-bash: cannot parse hook payload. Blocking (fail closed)." >&2
    exit 2
  fi
else
  echo "guard-bash: needs jq or python3. Run: sudo apt install jq" >&2
  exit 2
fi
# Split the output into lines: cwd, count, pieces, then the command (which
# may itself span lines). A malformed count is a parse failure.
mapfile -t lines <<<"$out"
pcwd=${lines[0]:-}
ntok=${lines[1]:-}
if ! [[ "$ntok" =~ ^(-1|[0-9]+)$ ]] || (( ${#lines[@]} < 2 + (ntok > 0 ? ntok : 0) )); then
  echo "guard-bash: cannot parse hook payload. Blocking (fail closed)." >&2
  exit 2
fi
PRE_PIECES=()
if (( ntok >= 0 )); then
  PRE_PIECES=( "${lines[@]:2:ntok}" )
  rest=$(( 2 + ntok ))
else
  rest=2
fi
if (( ${#lines[@]} > rest )); then
  printf -v cmd '%s\n' "${lines[@]:rest}"
  cmd=${cmd%$'\n'}
else
  cmd=""
fi
unset lines out
[[ "$pcwd" == /* ]] || pcwd="$PWD"

[[ -z "$cmd" ]] && exit 0

# --- escape hatch: session ------------------------------------------------
[[ "${HOOK_ALLOW_DESTRUCTIVE:-0}" == "1" ]] && exit 0

block() {
  cat >&2 <<EOF
BLOCKED by guard-bash: $1

Command: $cmd

This hook refuses destructive infrastructure commands. Do not retry the same
command or look for an unguarded route. If this operation is genuinely
required, stop and ask the operator to run it themselves in a terminal, or to
re-issue it prefixed with ALLOW_DESTRUCTIVE=1 — do not add that prefix on
your own initiative.
EOF
  exit 2
}

# --- push rules (the inline prefix does not lift these) -------------------
# A push that forces, or that writes or deletes the default branch, is
# refused. These run BEFORE the ALLOW_DESTRUCTIVE prefix is honoured: the
# model can type that prefix itself, so for a rule that must hold it is no
# override at all. Only an exported HOOK_ALLOW_DESTRUCTIVE lifts them.
# Decision and accepted residuals:
# docs/adr/0036-guard-bash-refuses-force-pushes-and-pushes-to-the-default-branch.md.
block_push() {
  cat >&2 <<EOF
BLOCKED by guard-bash: $1

Command: $cmd

Force-pushing, and pushing to or deleting the default branch, are refused.
Every change reaches the default branch through a pull request; a rewrite of
a pull-request branch uses --force-with-lease. The ALLOW_DESTRUCTIVE prefix
does not lift this rule. Do not retry it by another route; if it is genuinely
required, stop and ask the operator to run it themselves.
EOF
  exit 2
}

# The default branch is taken as main or master. Reading origin/HEAD would
# need git itself, an external command this guard otherwise never runs.
# git also guesses a destination's namespace (`heads/main`, `refs/main` both
# land on refs/heads/main), so those prefixes are peeled off first. Compared
# without case: on a case-insensitive filesystem `Main` is the same ref.
is_default_branch() {
  local n="${1,,}" p
  while :; do
    p=$n
    n=${n#refs/}
    n=${n#heads/}
    [[ "$n" == "$p" ]] && break
  done
  [[ "$n" == main || "$n" == master ]]
}

# Resolve directory $2 against $1 with builtins only (cd and pwd in a
# subshell). A leading ~ is the shell's, so it is expanded here too. Prints
# nothing and fails when the directory does not exist.
resolve_dir() {
  local d="$2"
  # shellcheck disable=SC2088  # the literal tilde is what is being matched
  case "$d" in
    '~')   d="${HOME:-}" ;;
    '~/'*) d="${HOME:-}/${d#\~/}" ;;
  esac
  [[ -n "$d" ]] || return 1
  [[ "$d" == /* ]] || d="$1/$d"
  (cd -- "$d" 2>/dev/null && pwd -P)
}

# The branch HEAD names in the repository containing directory $1, read from
# its HEAD file: the branch, or "" when detached. Fails when no repository
# can be found or read — the caller then refuses, since it cannot tell.
head_branch() {
  local d git line=""
  d=$(resolve_dir / "$1") || return 1
  while [[ ! -e "$d/.git" ]]; do
    [[ "$d" == / ]] && return 1
    d=${d%/*}
    [[ -z "$d" ]] && d=/
  done
  git="$d/.git"
  if [[ -f "$git" ]]; then          # a worktree or submodule: "gitdir: <path>"
    IFS= read -r line <"$git" || [[ -n "$line" ]] || return 1
    [[ "$line" == "gitdir: "* ]] || return 1
    git=${line#gitdir: }
    [[ "$git" == /* ]] || git="$d/$git"
    line=""
  fi
  IFS= read -r line <"$git/HEAD" 2>/dev/null || [[ -n "$line" ]] || return 1
  case "$line" in
    "ref: refs/heads/"*) printf '%s' "${line#ref: refs/heads/}" ;;
    "ref: "*)            return 1 ;;
    *)                   printf '' ;;
  esac
}

# Classify one git push long option by name, abbreviations included: git
# accepts any unique prefix (`--mirr` is --mirror), so a name that is a prefix
# of a dangerous option is taken as that option. A prefix of two options is
# an error to git, so refusing it costs nothing. Sets $lo.
lo=""
long_opt() {
  local o="${1%%=*}" k
  lo=ok
  case "$o" in
    force-with-lease|no-force-with-lease|force-if-includes|no-force-if-includes)
      return 0 ;;
  esac
  for k in force mirror all branches prune delete; do
    [[ "$k" == "$o"* ]] && { lo=$k; return 0; }
  done
  if (( ${#o} >= 3 )) && [[ repo == "$o"* ]]; then lo=repo; return 0; fi
  if (( ${#o} >= 2 )); then
    for k in push-option receive-pack exec; do
      [[ "$k" == "$o"* ]] && { lo=arg; return 0; }
    done
  fi
  return 0
}

# A ref operand the shell would still expand is not the text the guard sees,
# so it cannot be judged: a variable, a substitution, a glob or a brace list.
unjudgeable_ref() {
  [[ -z "$1" || "$1" == *[\$\`\*\?\[\{]* ]]
}

# Directory the next segment runs in: the payload's cwd, moved by `cd`.
# Empty means unknown, which a bare push then refuses.
vcwd="$pcwd"
sub=0            # depth of ( … ) subshells the walk is inside
last_cd=0        # the previous segment was a cd

# A prefix assignment: NAME=, NAME+= or NAME[i]=.
ASSIGN='^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?='

# git reads its repository and configuration from the environment too, so an
# assignment naming one is remembered for a push. Sets judge's own
# env_dir / env_cfg (bash locals are visible to the functions they call).
note_assignment() {
  local name="${1%%[+=[]*}"
  case "$name" in
    GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR) env_dir=1 ;;
    GIT_CONFIG*)                          env_cfg=$name ;;
  esac
}

# judge <continued>: judge one simple command, given as the words in TK; when
# it is a git push, refuse a force spelling or a destination that is the
# default branch. <continued> is 1 when the command runs on into a
# backquoted substitution, so its last operand is not all the guard can see.
TK=()
judge() {
  local -a tk=( "${TK[@]}" ); local i=0 j w a dir n v
  local wrapped=0 wrap_cd=0 xa=0 env_dir=0 env_cfg="" cont="${1:-0}"
  n=${#tk[@]}
  last_cd=0

  # Skip assignments (each noted by note_assignment; a `+=` is one too, never
  # the command word), shell keywords and wrappers to reach the command word.
  while (( i < n )); do
    w=${tk[i]}
    if [[ "$w" =~ $ASSIGN ]]; then
      note_assignment "$w"
      (( i++ )); continue
    fi
    case "$w" in
      sudo|env|exec|nice|ionice|stdbuf|xargs|timeout|time)
        wrapped=1
        [[ "$w" == xargs ]] && xa=1 ;;
      command|builtin|nohup|eval|'{'|'!'|then|do|else|if|while|until) ;;
      *) break ;;
    esac
    (( i++ ))
    while (( i < n )) && [[ "${tk[i]}" == -* ]]; do
      (( wrapped )) && [[ "${tk[i]}" == -C* || "${tk[i]}" == --chdir* ]] && wrap_cd=1
      (( i++ ))
    done
  done
  # Those wrappers' options can take values (`nice -n 5 git`, `sudo -u x
  # git`, `timeout -s KILL 5 git`), so after one the command word is the first
  # token named git, wherever it is. A -C or --chdir on the way (`env -C dir`)
  # moves the directory to one this walk does not follow.
  if (( wrapped )) && [[ "${tk[i]##*/}" != git ]]; then
    while (( i < n )) && [[ "${tk[i]##*/}" != git ]]; do
      [[ "${tk[i]}" == -C* || "${tk[i]}" == --chdir* ]] && wrap_cd=1
      [[ "${tk[i]}" =~ $ASSIGN ]] && note_assignment "${tk[i]}"
      (( i++ ))
    done
  fi

  # A command word the guard cannot read is not assumed not to be git. That
  # is a variable or substitution (`$g push`, `$(command -v git) push`), a
  # glob or brace list the shell expands (`/usr/bin/gi? push`), the `push`
  # or empty word a split substitution leaves behind, or a wrapper whose git
  # never appears literally. When the command carries a `push` operand it is
  # refused (fail closed), as a refspec the shell expands is. `[` and `[[`
  # are the test command, not a glob.
  j=-1
  if (( i < n )); then
    w=${tk[i]}
    if [[ -z "$w" || "${w##*/}" == push ]] \
       || [[ "$w" != '[' && "$w" != '[[' && "$w" == *[\$\`\*\?\[\{]* ]]; then
      j=$i
    fi
  elif (( wrapped )); then
    for (( j=0; j<n; j++ )); do
      [[ "${tk[j]}" == *[\$\`\*\?\[\{]* ]] && break
    done
  fi
  for (( ; j>=0 && j<n; j++ )); do
    [[ "${tk[j]}" == push ]] \
      && block_push "git push through a command word the guard cannot read (fail closed)"
  done
  (( i < n )) || return 0
  w=${tk[i]##*/}

  case "$w" in
    cd|pushd)
      last_cd=1
      (( i++ ))
      while (( i < n )) && [[ "${tk[i]}" == -* && "${tk[i]}" != - ]]; do (( i++ )); done
      if (( i >= n )); then
        v="${HOME:-}"
      else
        v=${tk[i]}
      fi
      if (( sub )) || [[ -z "$vcwd" || "$v" == - || "$v" == *'$'* ]]; then
        vcwd=""
      else
        vcwd=$(resolve_dir "$vcwd" "$v") || vcwd=""
      fi
      return 0 ;;
    popd)
      vcwd=""; return 0 ;;
    bash|sh|dash|zsh|ksh)
      # `bash -c 'git push …'`: the code is the rest of the command, scanned
      # as a command of its own. It runs in a child shell, so a `cd` in it
      # does not move this one (scan_command puts the directory back).
      (( i++ ))
      while (( i < n )) && [[ "${tk[i]}" == -* ]]; do
        if [[ "${tk[i]}" =~ ^-[A-Za-z]*c[A-Za-z]*$ ]]; then
          scan_command "${tk[*]:i+1}"
          return 0
        fi
        (( i++ ))
      done
      return 0 ;;
    git) ;;
    *) return 0 ;;
  esac

  # git's global options, before the subcommand. Inline configuration that
  # can redirect or rename a push is refused outright: it is typed in the
  # command, so it is the model's to choose.
  dir="$vcwd"
  (( wrap_cd || env_dir )) && dir=""
  (( i++ ))
  while (( i < n )); do
    a=${tk[i]}
    case "$a" in
      -C)
        if [[ -n "$dir" ]]; then dir=$(resolve_dir "$dir" "${tk[i+1]:-}") || dir=""; fi
        (( i+=2 )); continue ;;
      -C?*)
        if [[ -n "$dir" ]]; then dir=$(resolve_dir "$dir" "${a#-C}") || dir=""; fi ;;
      --git-dir|--work-tree)
        dir=""; (( i+=2 )); continue ;;
      --git-dir=*|--work-tree=*)
        dir="" ;;
      -c|--config-env)
        v=${tk[i+1]:-}
        v=${v,,}
        case "$v" in
          push.*|remote.*|alias.*|"")
            block_push "git -c ${v%%=*} (inline configuration that can redirect a push)" ;;
        esac
        (( i+=2 )); continue ;;
      --config-env=*)
        v=${a#--config-env=}
        v=${v,,}
        case "$v" in
          push.*|remote.*|alias.*|"")
            block_push "git --config-env ${v%%=*} (inline configuration that can redirect a push)" ;;
        esac ;;
      --namespace|--exec-path|--super-prefix|--attr-source)
        (( i+=2 )); continue ;;
      -*) ;;
      *) break ;;
    esac
    (( i++ ))
  done
  # A subcommand the shell builds (`git $(echo push)`, `git $sub`, a glob)
  # cannot be told from push, so it is refused (fail closed).
  v=${tk[i]:-}
  if (( i < n )) && [[ -z "$v" || "$v" == *[\$\`\*\?\[\{]* ]]; then
    block_push "git with a subcommand the guard cannot read ($v; fail closed)"
  fi
  [[ "$v" == push ]] || return 0
  (( i++ ))
  (( cont )) && block_push "git push with a command substitution in its operands (fail closed)"
  (( xa ))   && block_push "git push under xargs (its operands arrive at run time; fail closed)"
  [[ -n "$env_cfg" ]] \
    && block_push "git push with $env_cfg set (configuration in the environment can redirect a push)"

  # git push's own options and operands. The first operand is always the
  # repository, even beside --repo (git lets the operand override it).
  local force=0 del=0 every=0 mirror=0 prune=0 seen_remote=0 r src dst cur
  local -a refs=()
  while (( i < n )); do
    a=${tk[i]}
    case "$a" in
      --)
        (( i++ ))
        while (( i < n )); do
          if (( seen_remote )); then refs+=( "${tk[i]}" ); else seen_remote=1; fi
          (( i++ ))
        done
        break ;;
      --*)
        long_opt "${a#--}"
        case "$lo" in
          force)           force=1 ;;
          mirror)          mirror=1 ;;
          all|branches)    every=1 ;;
          prune)           prune=1 ;;
          delete)          del=1 ;;
          repo|arg)        [[ "$a" == *=* ]] || (( i++ )) ;;
        esac ;;
      -*)
        # A short cluster, read letter by letter: `o` takes a value, the
        # rest of the token (`-oci.skip`) or else the next token (`-uo x`).
        v=${a#-}
        while [[ -n "$v" ]]; do
          case "${v:0:1}" in
            f) force=1 ;;
            d) del=1 ;;
            o) [[ -z "${v:1}" ]] && (( i++ )); break ;;
          esac
          v=${v:1}
        done ;;
      *)
        if (( seen_remote )); then refs+=( "$a" ); else seen_remote=1; fi ;;
    esac
    (( i++ ))
  done

  (( force ))  && block_push "git push --force (use --force-with-lease on a pull-request branch)"
  (( mirror )) && block_push "git push --mirror (overwrites and deletes remote refs)"
  (( prune ))  && block_push "git push --prune (can delete the default branch)"
  (( every ))  && block_push "git push --all / --branches (pushes the default branch)"

  for r in "${refs[@]}"; do
    unjudgeable_ref "$r" \
      && block_push "git push with a refspec the shell would expand ($r; fail closed)"
    [[ "$r" == +* ]] && block_push "git push +refspec (a forced update: $r)"
    if (( del )); then
      dst=$r
    elif [[ "$r" == *:* ]]; then
      src=${r%%:*}
      dst=${r#*:}
      [[ -z "$dst" ]] && dst=$src
    else
      src=$r
      dst=$r
    fi
    if [[ "$dst" == HEAD || "$dst" == @ ]]; then
      [[ -n "$dir" ]] && cur=$(head_branch "$dir") \
        || block_push "git push of HEAD from a directory whose branch cannot be read (fail closed)"
      dst=$cur
    fi
    is_default_branch "$dst" && block_push "git push to the default branch ($dst)"
  done

  if (( ${#refs[@]} == 0 )); then
    # No refspec: git pushes the current branch (push.default=simple).
    [[ -n "$dir" ]] && cur=$(head_branch "$dir") \
      || block_push "bare git push from a directory whose branch cannot be read (fail closed)"
    is_default_branch "$cur" && block_push "bare git push while on the default branch ($cur)"
  fi
  return 0
}

# --- two passes over the command ---------------------------------------------
# Every command is judged twice, and each pass refuses on its own, so the
# union only ever refuses more:
#
# - the naive pass splits on separators without respecting quotes. It sees
#   inside a quoted substitution (`echo "$(git push origin main)"`) and a
#   here-document's lines, which the quote-aware pass keeps as one word;
# - the quote-aware pass reads quotes as the shell does. A quoted `>` is an
#   ordinary character, a quoted `;` does not end the command, and a string
#   that spans spaces (`"/tmp/r >"`) is one word, none of which the naive
#   pass can tell.

# naive_words <segment>: whitespace words with quotes and backslashes removed,
# then redirections dropped: `main>/dev/null` is the operand `main`, and
# `2>`, `>log` or a lone `>` with the word after it go. Sets TK.
naive_words() {
  local -a raw=(); local a pre tgt drop=0
  read -ra raw <<<"$1"
  TK=()
  for a in "${raw[@]}"; do
    a=${a//[\"\'\\]/}
    if (( drop )); then drop=0; continue; fi
    if [[ "$a" == *[\<\>]* ]]; then
      pre=${a%%[<>]*}
      tgt=${a#*[<>]}
      tgt=${tgt#"${tgt%%[!<>&|]*}"}       # the rest of the operator, any length
      [[ -z "$tgt" ]] && drop=1
      [[ -z "$pre" || "$pre" =~ ^[0-9]+$ || "$pre" == '&' ]] && continue
      a=$pre
    fi
    TK+=( "$a" )
  done
}

# naive_pass <command>: each separator is kept at the start of the segment it
# opens, so the walk can tell what a `cd` was joined to: `&&` and `;` move the
# shell; a pipe, `||`, a background `&` or a subshell leave its directory
# unknown. A redirection carrying `&` (`2>&1`, `&>file`, `>&2`, `|&`) is not
# a background job or a separator, so it is folded to its plain form first.
naive_pass() {
  local -a segs=(); local s=$1 x line lead prev next k cont
  s=${s//'&>'/'>'}
  s=${s//'>&'/'>'}
  s=${s//'<&'/'<'}
  s=${s//'|&'/'|'}
  for x in ';' '&' '|' '(' ')' '`'; do s=${s//"$x"/$'\n'"$x"}; done
  while IFS= read -r line; do segs+=( "$line" ); done <<<"$s"
  for (( k=0; k<${#segs[@]}; k++ )); do
    line=${segs[k]}
    lead=${line:0:1}
    case "$lead" in
      '(') (( sub++ )) ;;
      ')') (( sub > 0 )) && (( sub-- )) ;;
      '|') (( last_cd )) && vcwd="" ;;
      '&')
        prev=""
        (( k > 0 )) && prev=${segs[k-1]}
        (( last_cd )) && [[ "$line" != '&' && "$prev" != '&' ]] && vcwd="" ;;
    esac
    [[ "$lead" == [\;\&\|\(\)\`] ]] && line=${line:1}
    next=${segs[k+1]:-}
    cont=0
    [[ "${next:0:1}" == '`' ]] && cont=1
    naive_words "$line"
    judge "$cont"
  done
}

# get_pieces <command> <pre>: the command's tokeniser pieces, into PIECES.
# With <pre> 1 the payload parse already tokenised it (PRE_PIECES); otherwise,
# a `bash -c` code string or a command the parse left untokenised, the
# tokeniser runs here, python3 preferred. A tokeniser that fails, or jq past
# its limit with no python3, refuses the push (fail closed).
get_pieces() {
  local out
  if (( $2 )); then
    PIECES=( "${PRE_PIECES[@]}" )
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    out=$(printf '%s' "$1" | python3 -I -c "$TOK_PY"'
import sys
sys.stdout.write("".join(p + "\n" for p in tok(sys.stdin.read())))' 2>/dev/null) \
      || block_push "the quote-aware tokeniser failed on a command that names push (fail closed)"
  elif (( ${#1} <= TOK_JQ_MAX )); then
    out=$(printf '%s' "$1" | jq -Rsr "def tok: $TOK_JQ; tok" 2>/dev/null) \
      || block_push "the quote-aware tokeniser failed on a command that names push (fail closed)"
  else
    block_push "a command over $TOK_JQ_MAX characters that names push needs python3 to be judged (fail closed)"
  fi
  PIECES=()
  [[ -n "$out" ]] && mapfile -t PIECES <<<"$out"
}

# assemble: from PIECES, set QT to the shell words and operators ("W<word>",
# "O<operator>"), a redirection's target and any comment dropped; and set
# NAIVE_TEXT to the command with its comments removed and its quotes gone,
# which is what the naive pass splits. Taking the naive pass's text from the
# pieces is what lets it skip a comment (`echo done #; git push origin main`
# runs no push) while still seeing inside a quoted substitution, whose text
# stays in the Q piece.
assemble() {
  local p v w="" have=0 skip=0
  QT=()
  NAIVE_TEXT=""
  for p in "${PIECES[@]}"; do
    v=${p:1}
    case "${p:0:1}" in
      W) NAIVE_TEXT+=" "
         if (( have )); then
           if (( skip )); then skip=0; else QT+=( "W$w" ); fi
         fi
         w=""; have=0 ;;
      C) ;;
      R|O)
         NAIVE_TEXT+=" $v "
         if (( have )); then
           if (( skip )); then skip=0; else QT+=( "W$w" ); fi
         fi
         w=""; have=0
         if [[ "${p:0:1}" == R ]]; then skip=1; else skip=0; QT+=( "O$v" ); fi ;;
      Q|L) NAIVE_TEXT+=$v; w+=$v; have=1 ;;
    esac
  done
  if (( have && !skip )); then QT+=( "W$w" ); fi
}

# quote_pass: judge each simple command in QT between unquoted operators.
quote_pass() {
  local -a items=( "${QT[@]}" "O" ); local it op k cont
  TK=()
  for (( k=0; k<${#items[@]}; k++ )); do
    it=${items[k]}
    if [[ "$it" == W* ]]; then TK+=( "${it:1}" ); continue; fi
    op=${it:1}
    cont=0
    [[ "$op" == '`' ]] && cont=1
    (( ${#TK[@]} )) && judge "$cont"
    TK=()
    case "$op" in
      '(')               (( sub++ )) ;;
      ')')               (( sub > 0 )) && (( sub-- )) ;;
      '|'|'||'|'|&'|'&') (( last_cd )) && vcwd="" ;;
    esac
  done
}

# What makes a command worth judging, once quotes are removed: it names push,
# or it runs git with a subcommand the shell builds (an expansion in the first
# word after git's options and their values), which could be push
# (`git pu${x}sh`, `git -C dir $sub`). Anything else has nothing for these
# rules to judge, and costs no tokeniser run.
GIT_BUILT='(^|[^[:alnum:]_])git([[:space:]]+-[^[:space:]]*([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+[^[:space:]]*[$`*?[{]'

# scan_command <command> <pre>: both passes, each from the same starting
# directory; the directory and the walk's state are put back after (a `bash
# -c` code string runs in a child shell, so nothing in it moves this one).
# <pre> 1 means the payload parse tokenised this command already.
scan_command() {
  local start=$vcwd s0=$sub l0=$last_cd t nt
  local -a qt=()
  # The quote-stripping substitution is slow on a long command, so it runs
  # only when the plain text does not already name push.
  if [[ "$1" != *push* ]]; then
    t=${1//[\"\'\\]/}
    [[ "$t" == *push* || "$t" =~ $GIT_BUILT ]] || return 0
  fi
  get_pieces "$1" "${2:-0}"
  assemble
  qt=( "${QT[@]}" ); nt=$NAIVE_TEXT
  sub=0; last_cd=0
  naive_pass "$nt"
  vcwd=$start; sub=0; last_cd=0
  QT=( "${qt[@]}" )
  quote_pass
  vcwd=$start; sub=$s0; last_cd=$l0
}

# A backslash-newline continues the line, as the shell would join it. The
# top-level command uses the parse's pieces when it produced them.
pcmd=${cmd//$'\\\n'/ }
if (( ntok >= 0 )); then scan_command "$pcmd" 1; else scan_command "$pcmd" 0; fi

# Backstop over the whole string for spellings the segment parser cannot
# reach (an interpreter's inline code, a here-document): a bare --force or a
# short-flag cluster with an f before any o (after o the rest is -o's value),
# after `git push` (or `git -C x push`) and before
# the next separator or line, so a later command's -f is not read as the push's.
BACKSTOP='git[[:space:]]+((-C|-c)[[:space:]]*[^[:space:]]+[[:space:]]+)*push([[:space:]][^;&|'$'\n'']*)?[[:space:]](--force|-[a-np-zA-Z]*f[a-zA-Z]*)([[:space:]]|$)'
[[ "$pcmd" =~ $BACKSTOP ]] \
  && block_push "git push --force (use --force-with-lease on a pull-request branch)"

# --- escape hatch: one command ------------------------------------------
[[ "$cmd" =~ ^[[:space:]]*ALLOW_DESTRUCTIVE=1[[:space:]] ]] && exit 0

# --- rules --------------------------------------------------------------
# Ordered roughly by blast radius.

# A keyword only counts when it BEGINS a command word: start of string,
# whitespace, or a shell separator. Unanchored, `rm` matched inside any word
# ending -rm, so "Terraform from", "perform full" and `git commit -m "platform
# fixes"` were all blocked as a recursive delete. Same class for mkfs/dropdb.
CMD_START='(^|[[:space:];&|(])'
RM_CMD="${CMD_START}rm[[:space:]]+"

# Recursive force-delete outside obviously-scratch paths
if [[ "$cmd" =~ ${RM_CMD}(-[a-zA-Z]*[rR][a-zA-Z]*[[:space:]]+)*-?[a-zA-Z]*[fF] ]] ||
   [[ "$cmd" =~ ${RM_CMD}-[a-zA-Z]*[rR] ]]; then
  # Same catch, accurate reason: `docker rm -f` is a container removal, not a
  # recursive unlink. Narrowed to what the rm rule already caught -- a
  # top-level docker rule would also take `docker volume rm`, and widening the
  # guard is a separate decision from correcting a message.
  if [[ "$cmd" =~ ${CMD_START}(docker|podman)([[:space:]]+[a-z]+)*[[:space:]]+rm([[:space:]]|$) ]]; then
    block "docker/podman container removal"
  fi
  if ! [[ "$cmd" =~ (/tmp/|/var/tmp/|\./(build|dist|node_modules|__pycache__|\.venv|target)) ]]; then
    block "recursive rm outside a scratch path"
  fi
fi

# Whole-disk or device writes
[[ "$cmd" =~ dd[[:space:]].*of=/dev/ ]] && block "dd writing to a block device"
[[ "$cmd" =~ ${CMD_START}mkfs(\.[a-z0-9]+)?([[:space:]]|$) ]] && block "filesystem creation"

# GCP: anything that deletes, and the SQL/Run/secrets surface specifically
[[ "$cmd" =~ gcloud[[:space:]].*[[:space:]]delete([[:space:]]|$) ]] \
  && block "gcloud delete"
[[ "$cmd" =~ gcloud[[:space:]]+sql[[:space:]]+instances[[:space:]]+(patch|restart|failover|restore-backup) ]] \
  && block "mutating gcloud sql instances operation"
[[ "$cmd" =~ gcloud[[:space:]]+(beta[[:space:]]+)?billing ]] \
  && block "gcloud billing change"
[[ "$cmd" =~ gcloud[[:space:]]+secrets[[:space:]]+(destroy|delete|disable) ]] \
  && block "Secret Manager destruction"
[[ "$cmd" =~ gcloud[[:space:]]+projects[[:space:]]+(delete|set-iam-policy) ]] \
  && block "project deletion or IAM policy replacement"

# GCS bulk delete
[[ "$cmd" =~ (gsutil|gcloud[[:space:]]+storage)[[:space:]].*[[:space:]]rm[[:space:]] ]] \
  && [[ "$cmd" =~ (-r|--recursive|\*) ]] \
  && block "recursive or wildcard object delete"

# Terraform
[[ "$cmd" =~ terraform[[:space:]]+destroy ]]              && block "terraform destroy"
[[ "$cmd" =~ terraform[[:space:]]+apply.*-auto-approve ]] && block "terraform apply without review"
[[ "$cmd" =~ terraform[[:space:]]+state[[:space:]]+rm ]]  && block "terraform state rm"

# Kubernetes
[[ "$cmd" =~ kubectl[[:space:]]+delete ]] && block "kubectl delete"

# Git history rewriting. Pushes are handled by the push rules above.
[[ "$cmd" =~ git[[:space:]]+reset[[:space:]]+--hard ]] && block "git reset --hard"
[[ "$cmd" =~ git[[:space:]]+clean[[:space:]]+-[a-zA-Z]*[dfx] ]] && block "git clean"

# Piping the network straight into a shell
[[ "$cmd" =~ (curl|wget)[[:space:]].*\|[[:space:]]*(sudo[[:space:]]+)?(ba)?sh ]] \
  && block "piping a downloaded script into a shell"

# Database drops
#
# This rule was uppercase-only, and required whitespace on BOTH sides of the
# keyword. Bash =~ is case-sensitive, and quoted SQL puts a quote character
# before the keyword -- so it caught nothing it was written for, not even
# `psql -c "DROP TABLE orders"`. Match either case explicitly rather than with
# `shopt -s nocasematch`, which is global and would quietly relax every other
# rule in this file.
SQL_KEYWORD='([Dd][Rr][Oo][Pp]|[Tt][Rr][Uu][Nn][Cc][Aa][Tt][Ee])'
SQL_BEFORE='(^|[[:space:];&|("'"'"'])'
[[ "$cmd" =~ (psql|mysql)([[:space:]]|$).*${SQL_BEFORE}${SQL_KEYWORD}([[:space:]]|$) ]] \
  && block "SQL DROP/TRUNCATE"
[[ "$cmd" =~ ${CMD_START}dropdb([[:space:]]|$) ]] && block "dropdb"

exit 0
