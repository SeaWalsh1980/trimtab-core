#!/usr/bin/env bash
# PreToolUse hook, matcher: Write|Edit|MultiEdit
#
# Blocks writes to credential files, and writes whose *content* looks like a
# live secret. Exit 2 = blocked, stderr is fed back to Claude.
#
# Two tiers of content rules:
#   1. Specific credential signatures (PEM, service-account JSON, AWS, GitHub,
#      Telegram, JWT, generic sk- keys), plus the signature packs in
#      secrets.d/ and the private patterns in TRIMTAB_SECRET_PATTERNS. With
#      TRIMTAB_SECRET_PACKS unset or empty, every shipped pack applies; a
#      comma-separated list narrows it; `none` leaves only the built-ins —
#      enforced EVERYWHERE, including test files. A real token in a fixture is
#      a leak.
#   2. Fuzzy heuristics (long value on a secret-named variable, DSN with an
#      inline password) — relaxed under test/fixture paths and for
#      localhost DSNs, because fake credentials there are normal and the
#      noise trains people to bypass the guard.
#
# Fails CLOSED: a payload it cannot parse, an empty payload, and a scan it
# cannot run all block. Claude Code treats every exit code except 2 as
# non-blocking, so a guard that errors out looks identical to a guard that is
# allowing everything. Its externals are therefore checked, not assumed: jq or
# python3 to parse, and grep to scan. Everything else is a bash builtin, by
# design — head/tail/basename were dropped for expansions after a missing one
# was found to empty the scan and allow the write.
# See docs/adr/0001-guards-fail-closed-on-missing-dependencies.md.
#
# Session override:  export HOOK_ALLOW_SECRETS=1
#   (must be exported before launching claude — an inline env prefix on the
#    Bash tool's command never reaches this process)
# Per-line override: put the marker  hook:allow-secret  in a comment on the
#                    same line as the value.

set -uo pipefail

[[ "${HOOK_ALLOW_SECRETS:-0}" == "1" ]] && exit 0

payload=$(cat)

# An empty payload is not a harmless tool call. jq reports empty stdin as a
# successful parse of nothing, so the scan below would find nothing to object
# to and allow the write. Truncated stdin and a missing `cat` both land here.
if [[ -z "$payload" ]]; then
  echo "guard-secrets: empty hook payload. Blocking (fail closed)." >&2
  exit 2
fi

# grep is as much a dependency as jq/python3, and until it was checked here a
# missing one produced an empty scan and exit 0 — a silent allow whose only
# trace was "grep: command not found" on stderr, indistinguishable from clean
# content. The other externals this script used (head, tail, basename) are now
# bash expansions instead, so grep is the whole list.
if ! command -v grep >/dev/null 2>&1; then
  echo "guard-secrets: needs grep to scan content. Blocking (fail closed)." >&2
  exit 2
fi

# Write -> .content, Edit -> .new_string, MultiEdit -> every edit's new_string.
# Emits the path on line 1, then the concatenated new content.
extract() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '
      (.tool_input.file_path // ""),
      ([ .tool_input.content?,
         .tool_input.new_string?,
         (.tool_input.edits? // [] | .[].new_string?)
       ] | map(select(. != null)) | join("\n"))
    '
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
ti = json.load(sys.stdin).get("tool_input", {}) or {}
parts = [ti.get("content"), ti.get("new_string")]
parts += [e.get("new_string") for e in (ti.get("edits") or [])]
print(ti.get("file_path", "") or "")
print("\n".join(p for p in parts if p))
'
  else
    echo "guard-secrets: needs jq or python3. Run: sudo apt install jq" >&2
    exit 2
  fi
}

if ! extracted=$(printf '%s' "$payload" | extract 2>/dev/null); then
  echo "guard-secrets: cannot parse hook payload. Blocking (fail closed)." >&2
  exit 2
fi
# Split the extractor's two lines with bash expansions rather than head/tail:
# a coreutils binary missing from PATH used to empty BOTH variables, and an
# empty content falls through to the allow path at the end of the file.
# Command substitution has already stripped the trailing newline, so a payload
# with no content arrives as a single line.
if [[ "$extracted" == *$'\n'* ]]; then
  path=${extracted%%$'\n'*}
  content=${extracted#*$'\n'}
else
  path=$extracted
  content=""
fi

block() {
  cat >&2 <<EOF
BLOCKED by guard-secrets: $1

Target: ${path:-<unknown>}

Credentials should not be written by an agent. Put the real value in Secret
Manager (or your local secret store) and reference it by name; commit only a
placeholder or a .example file. Do not retry with the value obfuscated or
split across lines.

If this is a deliberately fake value the operator wants written as-is, ask
them to confirm; the sanctioned overrides are the  hook:allow-secret  marker
in a comment on the same line, or exporting HOOK_ALLOW_SECRETS=1 before
launching claude. Do not apply either override on your own initiative.
EOF
  exit 2
}

# A scan that could not run is not a clean scan. Distinct from block() because
# nothing was detected: the guard is refusing to answer, not answering no.
# printf rather than a cat heredoc so this still speaks when PATH is broken.
fail_closed() {
  printf '%s\n' \
    "BLOCKED by guard-secrets: $1" \
    "" \
    "Target: ${path:-<unknown>}" \
    "" \
    "The content could not be scanned, so the guard cannot say it is clean." \
    "This is a fail-closed block, not a detection. Report it to the operator" \
    "rather than retrying — the guard's dependencies are broken." >&2
  exit 2
}

# --- path rules ---------------------------------------------------------
if [[ -n "$path" ]]; then
  base=${path##*/}

  # Allow the documentation-shaped versions
  if ! [[ "$base" =~ \.(example|sample|template|dist)$ || "$base" =~ ^\.env\.(example|sample|template)$ ]]; then
    [[ "$base" =~ ^\.env(\..*)?$ ]]                    && block "write to a .env file"
    [[ "$base" =~ \.(pem|key|p12|pfx|jks|keystore)$ ]] && block "write to a private key file"
    [[ "$base" =~ ^id_(rsa|dsa|ecdsa|ed25519)$ ]]      && block "write to an SSH private key"
    [[ "$base" =~ ^(service-account|serviceaccount|gcp-key|credentials).*\.json$ ]] \
      && block "write to a service-account key file"
    [[ "$base" =~ ^\.(netrc|pgpass|npmrc|pypirc|htpasswd)$ ]] && block "write to a credential file"
  fi

  [[ "$path" =~ (^|/)(\.ssh|\.gnupg|\.aws|\.config/gcloud)/ ]] \
    && block "write inside a credential directory"
  [[ "$path" =~ (^|/)secrets?/ ]] && [[ ! "$path" =~ \.(md|example|template)$ ]] \
    && block "write inside a secrets/ directory"
fi

[[ -z "$content" ]] && exit 0

# Are we in a test/fixture context? Only the fuzzy heuristics below relax;
# the specific signatures never do.
relaxed=0
case "$path" in
  */tests/*|*/test/*|*/fixtures/*|*/conftest.py|conftest.py) relaxed=1 ;;
  *) [[ "${path##*/}" == test_* ]] && relaxed=1 ;;
esac

# --- content rules ------------------------------------------------------
# grep's exit status: 0 = matched, 1 = no match, >=2 = grep itself failed.
# Only the first two are answers. The blanket `|| true` this file used could
# not tell them apart, and every call site then read a failure as "no match" —
# the fail-open. Input arrives by here-string rather than a pipe so the status
# is grep's own rather than pipefail's view of a two-process pipeline.
#
# Strip any line carrying the opt-out marker before scanning. Status 1 here
# means every line carried it, which leaves an empty scan and is allowed.
scan=$(grep -v 'hook:allow-secret' <<<"$content")
st=$?
(( st > 1 )) && fail_closed "grep exited $st while stripping hook:allow-secret markers"
[[ -z "$scan" ]] && exit 0

scan_matches() { # scan_matches <grep-args>...  -> 0 matched, 1 no match
  local st
  grep "$@" <<<"$scan"
  st=$?
  (( st > 1 )) && fail_closed "grep exited $st while scanning the content"
  return $st
}

hit() { scan_matches -Eq -e "$1"; }

# Tier 1: specific signatures — never relaxed.
hit '-----BEGIN [A-Z ]*PRIVATE KEY-----' \
  && block "PEM private key block in the content"

hit '"private_key"[[:space:]]*:[[:space:]]*"-----BEGIN' \
  && block "GCP service-account JSON with an embedded private key"

hit '\bAKIA[0-9A-Z]{16}\b' \
  && block "what looks like an AWS access key ID"

hit '\bgh[pousr]_[A-Za-z0-9]{36,}\b' \
  && block "what looks like a GitHub token"

hit '\b[0-9]{8,10}:AA[A-Za-z0-9_-]{33}\b' \
  && block "what looks like a Telegram bot token"

hit '\bsk-[A-Za-z0-9]{20,}\b' \
  && block "what looks like an API key"

hit '\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b' \
  && block "what looks like a signed JWT"

# Tier 1, continued: signature packs and private patterns (ADR 0008).
# Packs live in secrets.d/ beside this script. TRIMTAB_SECRET_PACKS unset or
# empty means every shipped pack applies (fail-safe: a machine whose settings
# lack the switch loses nothing); a comma-separated list narrows it; "none"
# leaves only the built-ins. TRIMTAB_SECRET_PATTERNS names an optional file
# of private patterns, given as an absolute path. Each line is "<ERE><TAB><label>". Anything declared
# but unusable, or a missing secrets.d/, fails closed (ADR 0001).
here=$(cd -P "${BASH_SOURCE[0]%/*}" 2>/dev/null && pwd -P) \
  || fail_closed "cannot locate its own directory to read signature packs"
packs_dir="$here/secrets.d"
[[ -d "$packs_dir" ]] || fail_closed "the signature packs directory secrets.d/ is missing beside the guard"
sources=()
case "${TRIMTAB_SECRET_PACKS:-}" in
  "")
    for f in "$packs_dir"/*.patterns; do
      [[ -e "$f" ]] && sources+=("$f")
    done ;;
  none) ;;
  *)
    IFS=',' read -r -a packs <<<"$TRIMTAB_SECRET_PACKS"
    for pack in "${packs[@]}"; do
      [[ "$pack" =~ ^[a-z0-9-]+$ && "$pack" != none ]] \
        || fail_closed "TRIMTAB_SECRET_PACKS names an invalid pack"
      [[ -r "$packs_dir/$pack.patterns" ]] \
        || fail_closed "signature pack '$pack' is enabled but has no secrets.d/$pack.patterns"
      sources+=("$packs_dir/$pack.patterns")
    done ;;
esac
if [[ -n "${TRIMTAB_SECRET_PATTERNS:-}" ]]; then
  # Absolute only: a relative value names a different file in every cwd, and
  # an empty copy there would silently disable every private pattern.
  [[ "$TRIMTAB_SECRET_PATTERNS" == /* ]] \
    || fail_closed "TRIMTAB_SECRET_PATTERNS is not an absolute path"
  [[ -r "$TRIMTAB_SECRET_PATTERNS" ]] || fail_closed "TRIMTAB_SECRET_PATTERNS names a file that cannot be read"
  sources+=("$TRIMTAB_SECRET_PATTERNS")
fi
for src in ${sources[@]+"${sources[@]}"}; do
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *$'\t'* ]] || fail_closed "a pattern line without a tab in ${src##*/}"
    re=${line%%$'\t'*}
    label=${line#*$'\t'}
    [[ -n "$re" && -n "$label" ]] || fail_closed "an empty pattern or label in ${src##*/}"
    hit "$re" && block "what looks like $label"
  done <"$src"
done

# Tier 2: fuzzy heuristics — skipped in test/fixture files.
if [[ $relaxed -eq 0 ]]; then
  # An assignment of a long opaque value to a secret-ish name.
  # Placeholders (<...>, ${...}, x's, CHANGEME, your-...) are allowed through.
  scan_matches -Eiq \
    '(secret|password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9/+_-]{20,}' \
    && ! scan_matches -Eiq \
      '[:=][[:space:]]*["'"'"']?(\$\{|<|xxx|placeholder|changeme|your[_-]|dummy|example|redacted|\.\.\.)' \
    && block "a long literal assigned to a secret-named variable"
fi

# postgres://user:password@host style URLs. Localhost DSNs are dev fixtures
# (docker-compose, alembic dev config) — only non-local hosts block.
dsns=$(grep -Eo \
  '(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp)://[^:/@[:space:]]+:[^@[:space:]]{8,}@[^/[:space:]"'"'"']*' \
  <<<"$scan")
st=$?
(( st > 1 )) && fail_closed "grep exited $st while scanning for connection strings"
if [[ -n "$dsns" ]]; then
  grep -Evq '@(localhost|127\.0\.0\.1)([:/]|$)' <<<"$dsns"
  st=$?
  (( st > 1 )) && fail_closed "grep exited $st while checking connection-string hosts"
  (( st == 0 )) && block "a connection string with an inline password"
fi

exit 0
