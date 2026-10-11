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
# both land here. Everything after this point is a bash builtin, so jq/python3
# and cat are this guard's entire external surface.
if [[ -z "$payload" ]]; then
  echo "guard-bash: empty hook payload. Blocking (fail closed)." >&2
  exit 2
fi

# Extract .tool_input.command using jq if present, else python3.
# A parse failure must be distinguishable from an empty command: the former
# blocks, the latter is allowed. Trusting the output alone conflates them.
if command -v jq >/dev/null 2>&1; then
  if ! cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null); then
    echo "guard-bash: cannot parse hook payload. Blocking (fail closed)." >&2
    exit 2
  fi
elif command -v python3 >/dev/null 2>&1; then
  if ! cmd=$(printf '%s' "$payload" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null); then
    echo "guard-bash: cannot parse hook payload. Blocking (fail closed)." >&2
    exit 2
  fi
else
  echo "guard-bash: needs jq or python3. Run: sudo apt install jq" >&2
  exit 2
fi

[[ -z "$cmd" ]] && exit 0

# --- escape hatches -----------------------------------------------------
[[ "${HOOK_ALLOW_DESTRUCTIVE:-0}" == "1" ]] && exit 0
[[ "$cmd" =~ ^[[:space:]]*ALLOW_DESTRUCTIVE=1[[:space:]] ]] && exit 0

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

# Git history rewriting on shared branches
[[ "$cmd" =~ git[[:space:]]+push.*(--force|-f)([[:space:]]|$) ]] \
  && ! [[ "$cmd" =~ --force-with-lease ]] \
  && block "git push --force (use --force-with-lease)"
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
