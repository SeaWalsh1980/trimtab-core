#!/usr/bin/env bash
# PreToolUse hook, matcher: Edit|Write|MultiEdit|NotebookEdit|Bash
#
# Security-control policy: an agent must not change a security control on its
# own initiative. Covers the files that configure CI, review and scanning in
# any project repo, and the GitHub API calls that change a repo's protection.
# Exit 2 = blocked, stderr is fed back to Claude. Exit 0 = allowed.
#
# Reading is NOT blocked — Read is not in the matcher, and in Bash only write
# verbs, redirects and mutating `gh` calls are inspected. That is a deliberate
# departure from guard-paths' noun-only Bash matching; the reasons and the
# accepted residuals are in
# docs/adr/0011-guard-security-blocks-changes-to-security-controls.md.
#
# Fails CLOSED: an empty or unparseable payload blocks, and a missing jq falls
# back to python3. See docs/adr/0001-guards-fail-closed-on-missing-dependencies.md.
#
# Session override: export HOOK_ALLOW_SECURITY=1
#   (must be exported before launching claude — an inline prefix on the Bash
#    tool's command never reaches this process). The operator's decision only.

set -uo pipefail   # deliberately not -e: an unexpected non-zero must not
                   # short-circuit into a silent allow

[[ "${HOOK_ALLOW_SECURITY:-0}" == "1" ]] && exit 0

payload=$(cat)

# jq reports empty stdin as a successful parse of nothing; see ADR 0001.
if [[ -z "$payload" ]]; then
  echo "guard-security: empty hook payload. Blocking (fail closed)." >&2
  exit 2
fi

# ---- parse -------------------------------------------------------------
# Emits: file path, command, tool_name — one line each, same layout and
# reasoning as guard-paths (tool_name third, so branch skew degrades to
# "enforce") — then the command split into QUOTE-AWARE segments, one per line.
#
# Unlike guard-paths, a newline in the command becomes " ; " rather than a
# space. This guard reads the command WORD of each segment, and joining
# `echo hi` and `rm .gitleaks.toml` into one line would hide the rm behind the
# echo. A backslash-newline continuation is joined first, as the shell would.
#
# The quote-aware split lives here, not in bash, for time: a character walk in
# bash is quadratic and took 9s on a 140 KB heredoc, past the hook's 10s
# timeout — and a timed-out hook does not block. As one regex scan in jq or
# python3 it is linear. It comes AFTER tool_name, so a branch that failed to
# emit it degrades to the naive split alone rather than shifting tool_name.
# `>|` and `&>` are rewritten to `>` first, or splitting on | and & would
# separate the redirect from its target.
parse() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '((.tool_input.command // "") | split("\\\n") | join(" ")
           | split("\n") | join(" ; ")) as $c
           | (.tool_input.file_path // .tool_input.notebook_path // .tool_input.path // ""),
             $c,
             ((.tool_name // "") | split("\n") | join(" ")),
             ($c | split(">|") | join(">") | split("&>") | join(">")
                 | [scan("\u0027[^\u0027]*\u0027|\"(?:[^\"\\\\]|\\\\.)*\"|[^\u0027\"]+|[\u0027\"]")]
                 | map(if length > 1 and (.[0:1] == "\u0027" or .[0:1] == "\"") then .
                       else reduce (";", "&", "|", "(", ")", "`") as $s (.; split($s) | join("\n"))
                       end)
                 | join(""))'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, re, sys
d = json.load(sys.stdin)
ti = d.get("tool_input", {}) or {}
c = (ti.get("command") or "").replace("\\\n", " ").replace("\n", " ; ")
print(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")
print(c)
print((d.get("tool_name") or "").replace("\n", " "))
q = c.replace(">|", ">").replace("&>", ">")
parts = re.findall(r"\x27[^\x27]*\x27|\"(?:[^\"\\]|\\.)*\"|[^\x27\"]+|[\x27\"]", q)
print("".join(p if len(p) > 1 and p[0] in "\x27\"" else re.sub(r"[;&|()`]", "\n", p)
              for p in parts))
'
  else
    return 1
  fi
}

if ! parsed=$(printf '%s' "$payload" | parse 2>/dev/null); then
  echo "guard-security: cannot parse payload (needs jq or python3). Blocking." >&2
  exit 2
fi

# Split with the `read` builtin, not guard-paths' `${v%%$'\n'*}` expansions:
# on a 140 KB heredoc, bash's glob matcher took 4s for `[[ $v == *$'\n'* ]]`
# and 0.85s for one `%%` — measured, and the difference between finishing
# inside the 10s hook timeout and failing open. `read` is still a builtin, so
# no external dependency returns. Missing lines leave the variable empty.
file="" cmd="" tool="" segq=""
{
  IFS= read -r file
  IFS= read -r cmd
  IFS= read -r tool
  IFS= read -r -d '' segq
} <<<"$parsed"

deny() {
  cat >&2 <<EOF
BLOCKED by guard-security: $1
Target: $2

This is a security control. An agent must not change one on its own
initiative — not by editing the file, not by rewriting it from the shell, and
not through the GitHub API.

Reading is not blocked: Read, Grep, Glob, cat, git diff and gh api GETs work.

If this change is genuinely needed, stop and ask the operator, naming the
control. Only the operator can lift this block, by exporting
HOOK_ALLOW_SECURITY=1 before launching claude; an inline prefix on a Bash
command never reaches this hook. Do not set it yourself, and do not look for
another route.
EOF
  exit 2
}

# ---- what counts as a security control ---------------------------------
# Matched on a normalised absolute path, case-sensitively (GitHub paths are).
# Docs are deliberately absent, so the friction stays on real controls.
#
# CODEOWNERS matches at any depth: GitHub honours .github/, the repo root and
# docs/, first found wins, so deleting one promotes the next. `.github` and
# `.claude` themselves are listed so that `rm -r .github` or `mv .claude x`
# cannot remove the controls by removing their parent.
ctl=""
control() {
  local p="$1" base="${1##*/}"
  ctl=""
  case "$p" in
    */.github/workflows|*/.github/workflows/*)
      ctl="GitHub Actions workflows (.github/workflows)" ;;
    */.claude/settings.json|*/.claude/settings.local.json)
      ctl="Claude Code settings ($base)" ;;
    */.claude/hooks|*/.claude/hooks/*)
      ctl="Claude Code hooks (.claude/hooks)" ;;
    */.github|*/.claude)
      ctl="directory holding security controls ($base)" ;;
  esac
  if [[ -z "$ctl" ]]; then
    case "$base" in
      CODEOWNERS)
        ctl="CODEOWNERS (required reviewers)" ;;
      .gitleaks.toml|.trivyignore|.bandit-allowlist.yml)
        ctl="scanner exemption list ($base)" ;;
    esac
  fi
  [[ -n "$ctl" ]]
}

# Normalise to an absolute, lexically-resolved path against $2. Same approach
# as guard-paths' cp_norm: expand ~ and $HOME, resolve relative, collapse `..`.
n=""
norm() {
  local p="$1" base="$2" part reset_f=1
  local -a out=()
  case "$p" in
    '~')                p="${HOME:-}" ;;
    '~/'*)              p="${HOME:-}/${p#\~/}" ;;
    '$HOME'|'${HOME}')  p="${HOME:-}" ;;
    '$HOME/'*)          p="${HOME:-}/${p#\$HOME/}" ;;
    '${HOME}/'*)        p="${HOME:-}/${p#\$\{HOME\}/}" ;;
  esac
  [[ "$p" == /* ]] || p="$base/$p"
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
  n="/${out[*]}"
}

# The directory relative paths resolve against. Starts at the hook's cwd and
# follows `cd`/`pushd` through the command, so `cd .github/workflows && echo x
# > ci.yml` resolves ci.yml where the shell would.
vcwd="$PWD"

# check <raw-operand> <why>: deny if the operand names a control. Quotes and
# backslashes are stripped first (the shell would remove them too, so
# '.git''hub/…' is still .github/…). A glob is expanded against the filesystem,
# exactly as the shell would expand it: `rm .gitleak*` targets .gitleaks.toml.
check() {
  local raw="$1" why="$2" m
  raw=${raw//[\"\'\\]/}
  [[ -z "$raw" ]] && return 0
  norm "$raw" "$vcwd"
  if [[ "$n" == *[\*\?\[]* ]]; then
    while IFS= read -r m; do
      [[ -n "$m" ]] && control "$m" && deny "$ctl" "$why: $raw"
    done < <(compgen -G "$n" 2>/dev/null)
  fi
  control "$n" && deny "$ctl" "$why: $raw"
  return 0
}

# ---- Edit / Write / MultiEdit / NotebookEdit ---------------------------
# Allow-list of one, as in guard-paths: an empty or unknown tool enforces.
case "$tool" in
  Read) ;;
  *)    [[ -n "$file" ]] && check "$file" "file write" ;;
esac

[[ -z "$cmd" ]] && exit 0

# Inspection time grows with the command, and a hook that overruns its 10s
# timeout does not block — it fails open. Measured: 3.9s for a 140 KB heredoc
# dense with globs and `..` (the worst case, since those make every line worth
# inspecting). 100 000 characters keeps better than 3x headroom on a slower VM.
# Past it, block: content that size belongs in the Write tool, not a heredoc.
if (( ${#cmd} > 100000 )); then
  cat >&2 <<EOF
BLOCKED by guard-security: command too large to inspect (${#cmd} characters,
limit 100000) within the hook timeout. This is not a judgement about its
content. Write large content with the Write tool instead of a Bash heredoc.
EOF
  exit 2
fi

# ---- Bash --------------------------------------------------------------
# Segments: the command split on the shell's separators. Done twice — once
# naively (here), once respecting quotes (in parse) — and both are checked.
# Naive splitting breaks `sed -i "s/a/b/;s/c/d/" .gitleaks.toml` at the quoted
# `;`, so the second half has no command word; quote-aware splitting can be
# misled by an apostrophe in a heredoc body pairing with a later one. Each
# covers the other's miss, and the union only ever blocks more.

# Could this text reach a control at all? Every segment is tokenised to find
# its command word, but the operand and redirect checks run only when this
# says yes: a control is named, `..` could climb into one, a glob could expand
# onto one, or the virtual cwd is already a control directory. Without it a
# 3000-line heredoc cost 6s of per-line path normalisation for nothing.
relevant() {
  case "$vcwd" in
    */.github|*/.github/*|*/.claude|*/.claude/hooks|*/.claude/hooks/*) return 0 ;;
  esac
  case "$1" in
    *.github*|*.claude*|*CODEOWNERS*|*.gitleaks.toml*|*.trivyignore*|\
    *.bandit-allowlist.yml*|*..*|*[\*\?\[]*) return 0 ;;
  esac
  return 1
}

# Redirect targets within one segment: >, >>, and (after pre-normalisation
# below) >| and &>. `>&1` has no path target and is skipped by the class.
scan_redirects() {
  local s="$1" re='>>?[[:space:]]*([^[:space:]<>;&|()]+)'
  while [[ "$s" =~ $re ]]; do
    s=${s#*"${BASH_REMATCH[0]}"}
    check "${BASH_REMATCH[1]}" "shell redirect into"
  done
}

# Destination-only verbs: copying or linking a control OUT is a read. $2 is
# 1 when -t/--target-directory names the destination (cp, ln, install; for
# rsync -t means --times).
check_dest() {
  local verb="$1" has_t="$2"; shift 2
  local -a ops=("$@"); local i last=""
  for (( i=0; i<${#ops[@]}; i++ )); do
    case "${ops[i]}" in
      --target-directory=*) check "${ops[i]#*=}" "$verb onto"; return 0 ;;
      --target-directory)   check "${ops[i+1]:-}" "$verb onto"; return 0 ;;
      -t)  (( has_t )) && { check "${ops[i+1]:-}" "$verb onto"; return 0; } ;;
      -t?*) (( has_t )) && { check "${ops[i]#-t}" "$verb onto"; return 0; } ;;
      -*) ;;
      *)  last=${ops[i]} ;;
    esac
  done
  [[ -n "$last" ]] && check "$last" "$verb onto"
  return 0
}

check_all() {   # check_all <verb> <operand>...: every non-flag operand
  local verb="$1" o; shift
  for o in "$@"; do
    [[ "$o" == -* ]] && continue
    check "$o" "$verb"
  done
}

# gh: `gh api` mutations against protection endpoints, and the porcelain that
# changes the same settings. A GET is allowed; -f/-F/--input with no explicit
# method is a POST (gh's own rule), while `-X GET -f k=v` is a query string.
check_gh() {
  local -a a=() p=(); local x
  for x in "$@"; do a+=( "${x//[\"\'\\]/}" ); done
  for x in "${a[@]}"; do [[ "$x" == -* ]] || p+=( "$x" ); done

  case "${p[0]:-} ${p[1]:-}" in
    "secret set"|"secret delete"|"secret remove")
      deny "GitHub Actions secrets (gh secret ${p[1]})" "gh ${a[*]}" ;;
    "variable set"|"variable delete"|"variable remove")
      deny "GitHub Actions variables (gh variable ${p[1]})" "gh ${a[*]}" ;;
    "repo deploy-key")
      case "${p[2]:-}" in
        add|delete) deny "deploy keys (gh repo deploy-key ${p[2]})" "gh ${a[*]}" ;;
      esac ;;
  esac

  [[ "${a[0]:-}" == api ]] || return 0
  local method="" fields=0 ep="" i=1
  while (( i < ${#a[@]} )); do
    x=${a[i]}
    case "$x" in
      -X|--method)                         method=${a[i+1]:-}; (( i+=2 )); continue ;;
      --method=*)                          method=${x#--method=} ;;
      -X?*)                                method=${x#-X} ;;
      -f|-F|--field|--raw-field|--input)   fields=1; (( i+=2 )); continue ;;
      --field=*|--raw-field=*|--input=*|-f?*|-F?*) fields=1 ;;
      -H|--header|-q|--jq|-t|--template|--hostname|--cache|-p|--preview)
                                           (( i+=2 )); continue ;;
      -*) ;;
      *)  [[ -z "$ep" ]] && ep=$x ;;
    esac
    (( i++ ))
  done
  method=${method^^}
  case "$method" in
    PUT|PATCH|POST|DELETE) ;;
    "") (( fields )) || return 0 ;;
    *)  return 0 ;;
  esac

  local e="/${ep#/}"
  e="${e%%\?*}/"
  ctl=""
  case "$e" in
    */rulesets/*)                      ctl="repository rulesets" ;;
    */branches/*/protection/*)         ctl="branch protection" ;;
    */collaborators/*)                 ctl="repository collaborators" ;;
    */actions/secrets/*|*/environments/*/secrets/*|*/dependabot/secrets/*)
                                       ctl="Actions secrets" ;;
    */actions/variables/*|*/environments/*/variables/*)
                                       ctl="Actions variables" ;;
    */actions/permissions/*)           ctl="Actions permissions" ;;
    */hooks/*)                         ctl="webhooks" ;;
    */keys/*)                          ctl="deploy / SSH keys" ;;
  esac
  [[ -n "$ctl" ]] && deny "$ctl (gh api ${method:-POST})" "gh ${a[*]}"
  return 0
}

interp=0
scan_segment() {
  local seg="$1" w i=0 o
  local -a tk=()
  read -ra tk <<<"$seg"

  # Skip assignments and wrappers to reach the command word.
  while (( i < ${#tk[@]} )); do
    w=${tk[i]}
    if [[ "$w" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then (( i++ )); continue; fi
    case "$w" in
      sudo|env|command|builtin|exec|nohup|time|nice|ionice|stdbuf|xargs|timeout|\
      '{'|'!'|then|do|else|if|while|until)
        (( i++ ))
        while (( i < ${#tk[@]} )) && [[ "${tk[i]}" == -* ]]; do (( i++ )); done
        [[ "$w" == timeout ]] && (( i++ ))
        continue ;;
    esac
    break
  done
  (( i < ${#tk[@]} )) || return 0
  w=${tk[i]//[\"\'\\]/}
  w=${w##*/}
  local -a ops=( "${tk[@]:i+1}" )

  # Inline interpreter code. Its quoted body is split across segments by any
  # `;` inside it, so the check is deferred to a scan of the whole command.
  case "$w" in
    python|python[0-9]*|perl|node|ruby|php|deno|bash|sh|dash|zsh|ksh|eval)
      [[ "$w" == eval || "$seg" == *'<<'* ]] && interp=1
      for o in "${ops[@]}"; do
        [[ "$o" =~ ^-[A-Za-z]*[ceE]$ || "$o" == - || "$o" == --eval ]] && interp=1
      done ;;
  esac

  # cd and gh are always examined: cd moves the virtual cwd, and a gh mutation
  # names no path. Everything else only when it could reach a control.
  case "$w" in
    cd|pushd|gh) ;;
    *) relevant "$seg" || return 0
       scan_redirects "$seg" ;;
  esac

  case "$w" in
    cd|pushd)
      for o in "${ops[@]}"; do
        [[ "$o" == -* ]] && continue
        norm "${o//[\"\'\\]/}" "$vcwd"; vcwd=$n; return 0
      done
      vcwd="${HOME:-/}" ;;
    tee|mv|rm|unlink|shred|truncate|touch)
      check_all "$w" "${ops[@]}" ;;
    cp|ln|install)
      check_dest "$w" 1 "${ops[@]}" ;;
    rsync)
      check_dest "$w" 0 "${ops[@]}" ;;
    sed|perl)
      for o in "${ops[@]}"; do
        if [[ "$o" =~ ^-[A-Za-z]*i || "$o" == --in-place* ]]; then
          check_all "$w -i" "${ops[@]}"; break
        fi
      done ;;
    dd)
      for o in "${ops[@]}"; do
        [[ "$o" == of=* ]] && check "${o#of=}" "dd of="
      done ;;
    find)
      for o in "${ops[@]}"; do
        case "$o" in
          -delete|-exec|-execdir|-ok|-okdir) check_all "find $o" "${ops[@]}"; break ;;
        esac
      done ;;
    git)
      local sub="" j=0
      while (( j < ${#ops[@]} )); do
        case "${ops[j]}" in
          -C|-c) (( j+=2 )); continue ;;
          -*)    ;;
          *)     sub=${ops[j]}; break ;;
        esac
        (( j++ ))
      done
      case "$sub" in
        rm|mv|checkout|restore) check_all "git $sub" "${ops[@]:j+1}" ;;
      esac ;;
    gh)
      check_gh "${ops[@]}"
      relevant "$seg" && scan_redirects "$seg" ;;
  esac
  return 0
}

scan_list() {
  local seg
  vcwd="$PWD"
  while IFS= read -r seg; do
    scan_segment "$seg"
  done <<<"$1"
}

# Any path-like fragment anywhere in the command. Used only when inline
# interpreter code is present; conservative — it blocks a read of a control
# made through `python3 -c` too, which is the accepted price.
scan_fragments() {
  local c="$1" frag reset_f=1
  vcwd="$PWD"
  [[ -o noglob ]] && reset_f=0
  set -f
  local IFS=$' \t\n\047\042(),;|&<>={}:[]'
  for frag in $c; do
    [[ "$frag" == *[/.]* || "$frag" == CODEOWNERS ]] || continue
    relevant "$frag" || continue
    check "$frag" "inline interpreter code naming"
  done
  (( reset_f )) && set +f
  return 0
}

c=${cmd//'>|'/'>'}
c=${c//'&>'/'>'}
scan_list "${c//[;&|()\`]/$'\n'}"
scan_list "$segq"
(( interp )) && scan_fragments "$c"

exit 0
