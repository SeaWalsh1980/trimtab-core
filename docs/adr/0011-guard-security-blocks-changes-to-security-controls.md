# 0011. guard-security blocks changes to security controls

## Status

Accepted. Written for the base after the guard shipped, from its code and
tests, so that the decision has a record in this series.

## Context

Some files and settings exist to constrain or review change: CI workflows,
required reviewers, Claude Code settings and hooks, scanner exemption lists,
and a repository's protection, secrets and keys. An agent that can change one
of these can change what is allowed to happen next, so such a change must
never be incidental to a task.

Review alone does not catch it:

- claude-code-action skips a pull request that edits its own workflow and
  still reports success (ADR 0009), so a workflow edit can pass a required
  review check unreviewed.
- A change to `.claude/settings.json` or `.claude/hooks/` takes effect in the
  next session, with no pull request at all.
- Branch protection, rulesets, secrets and keys change through the GitHub API,
  not through a file.

The constraints:

- Controls must stay readable. Agents diagnose CI and review settings in every
  repository, and the friction belongs on changing a control, not on looking
  at one.
- Claude Code treats a hook that errors or times out as non-blocking, so the
  guard must fail closed (ADR 0001) and must finish inside its 10 s timeout.
- guard-paths already protects the live control plane, matching on paths and
  blocking any Bash command that names one, reads included (ADR 0002).

## Decision

Add `hooks/guard-security.sh`, a PreToolUse hook on
`Edit|Write|MultiEdit|NotebookEdit|Bash`, that blocks a change to a control
in any repository.

- **What counts as a control** is matched on a normalised absolute path:
  `.github/workflows/`, `.claude/settings.json`, `.claude/settings.local.json`,
  `.claude/hooks/`, the `.github` and `.claude` directories themselves (so
  removing the parent cannot remove the controls), `CODEOWNERS` at any depth,
  and `.gitleaks.toml`, `.trivyignore` and `.bandit-allowlist.yml`. Docs are
  deliberately absent.
- **File tools** are checked on their target path. Only `Read` is exempt, so
  an empty or unknown tool name enforces.
- **In Bash, verbs are inspected, not nouns.** The guard finds each segment's
  command word and checks only what can write: redirects, `tee`, `mv`, `rm`,
  `unlink`, `shred`, `truncate`, `touch`, the destination of `cp`, `ln`,
  `install` and `rsync`, `sed -i` and `perl -i`, `dd of=`, `find` with
  `-delete` or `-exec`, and `git rm|mv|checkout|restore`. `cd` and `pushd` move
  a virtual working directory so relative paths resolve where the shell would,
  and globs are expanded as the shell would expand them.
- **GitHub API mutations** are blocked by endpoint: rulesets, branch
  protection, collaborators, Actions secrets, variables and permissions,
  webhooks and keys, plus the `gh secret`, `gh variable` and
  `gh repo deploy-key` porcelain. A GET is allowed. Fields with no explicit
  method count as a POST, as gh itself treats them.
- **Inline interpreter code** (`-c`, `-e`, `eval`, or a heredoc into an
  interpreter) is scanned for any fragment naming a control, read or write.
- **It fails closed**: an empty or unparseable payload blocks, and a missing
  `jq` falls back to `python3` (ADR 0001).
- **It is lifted only by the operator**, by exporting `HOOK_ALLOW_SECURITY=1`
  before launching Claude Code. An inline prefix on a command never reaches
  the hook.

Three measurements shaped the implementation, all on a 140 KB heredoc:

| Approach | Time | Outcome |
| --- | --- | --- |
| Quote-aware split as a character walk in bash | 9 s | Too close to the 10 s timeout, after which the hook fails open; moved into one regex scan in `jq` or `python3`, which is linear |
| `[[ $v == *$'\n'* ]]` / one `${v%%…}` to split lines | 4 s / 0.85 s | Replaced by the `read` builtin |
| Whole guard, worst case (dense globs and `..`) | 3.9 s | Commands over 100 000 characters are blocked outright, keeping better than 3x headroom on a slower machine |

## Options considered

- **Match nouns, as guard-paths does: block any Bash command that names a
  control.** Rejected. Controls live in every repository and are read
  constantly; blocking `cat .github/workflows/ci.yml` or `git diff` on a
  workflow would put the friction on looking, not changing, and train the
  operator to switch the guard off. ADR 0002 makes the opposite choice for the
  live control plane, where the set of paths is small and closed; here it is
  open-ended and shared with ordinary work.
- **Block `Read` as well.** Rejected for the same reason, and because a
  control's content is not secret.
- **Rely on review and branch protection.** Rejected. Review can be skipped
  (ADR 0009), and settings, hooks and API changes need no pull request.
- **Rely on instruction text alone.** Rejected. A rule that must hold whatever
  the model decides needs a mechanism, not only an instruction.
- **An in-repository override (a marker file, a label, a commit token).**
  Rejected, as in ADR 0002: the agent the guard constrains could set it.

## Consequences

- Ordinary reads of a control pass guard-security: `Read`, `Grep`, `cat`,
  `git diff` and `gh api` GETs. The exception is inline interpreter code, below.
  Shell reads of the live control plane are still blocked by guard-paths
  (ADR 0002).
- Accepted residual: verb inspection cannot see every route to a write. Known
  routes it does not inspect include `git apply` and other git subcommands, a
  script run by path, `gh api graphql`, and a path held in a variable from an
  earlier assignment or command. The policy that an agent must not change a
  control on its own initiative still applies on those routes; only the hook
  cannot see them.
- Accepted price: inline interpreter code that names a control is blocked even
  when it only reads.
- A Bash command over 100 000 characters is blocked whatever it contains.
  Large content belongs in the Write tool.
- The guard matches `.claude/hooks/`, not a repository's top-level `hooks/`.
  This repository's `hooks/` is protected where it is live by guard-paths
  (ADR 0002), and treated as a control by policy elsewhere.
- The guard test matrix covers it with 68 cases in `tests/guard-tests.sh` at
  the time of writing.
