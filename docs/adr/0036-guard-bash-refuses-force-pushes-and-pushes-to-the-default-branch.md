# 0036. guard-bash refuses force pushes and pushes to the default branch

## Status

Accepted on merge (2026-10-20). No trial is pending: the guard matrix
exercises the decision in full. Changes a base expectation of the guard
matrix (`git push --force-with-lease origin main` now blocks), so the CI step
that runs the base branch's matrix against this change is red by design and
the change is merged over it.

## Context

A consuming project forbids pushing to its default branch and any force-push
other than `--force-with-lease` to a pull-request branch. Its branch ruleset
backs both, but a rule the agent must obey whatever it decides belongs in a
hook as well (the base rule that anything which must hold is enforced by a
hook, permission rule or test, not instruction text alone). guard-bash held
one regex for this:

    git[[:space:]]+push.*(--force|-f)([[:space:]]|$)   unless --force-with-lease appears

Each gap below was confirmed against that regex:

- `--force` passed whenever `--force-with-lease` also appeared in the command.
- Short-flag clusters (`-uf`, `-fu`) and `+refspec` (`origin +feat`,
  `+HEAD:feat`) were not matched.
- `git -C dir push --force` and `git -c k=v push --force` were not matched:
  the regex needed `git` and `push` adjacent.
- A push to the default branch was not covered at all, forced or not, and the
  matrix asserted that `git push --force-with-lease origin main` is allowed.
  Nor were deleting it (`:main`, `--delete main`), `--all`, `--mirror`,
  `--prune`, or a bare `git push` while on it.
- The inline `ALLOW_DESTRUCTIVE=1` prefix lifted every rule. The model can type
  that prefix itself, so for these rules it was no override at all.

## Decision

guard-bash parses each simple command (the command split on `; & | ( )` and
backquotes, wrappers such as `env`, `sudo`, `xargs`, `eval` skipped, and the
code of `bash -c` / `sh -c` re-parsed). For a `git … push`, after git's global
options (`-C` resolved, `-c` skipped, `--git-dir` / `--work-tree` making the
repository unknown), it refuses:

- any force spelling: `--force` (beside `--force-with-lease` too), a short
  cluster containing `f`, a `+refspec`, `--mirror`, `--prune`;
- `--all` / `--branches`;
- a destination that is `main` or `master`, with or without `refs/heads/`,
  including a delete (`:main`, `--delete`, `-d`);
- `HEAD` / `@` as the destination, or no refspec at all, when the current
  branch is `main` or `master`. The current branch is read from the
  repository's `HEAD` file (following a linked worktree's `.git` file), in the
  directory the payload's `cwd` names, moved by `cd` and `git -C`;
- that same case when the branch cannot be read — no repository, an
  unreadable `HEAD`, `cd -`, a `cd` through a variable, `--git-dir` — because
  "cannot tell" is not "safe" (ADR 0001).

A whole-string backstop also refuses `git push` followed by a bare `--force` or
an `f` cluster before the next separator, for spellings the segment parser
cannot reach, such as a here-document fed to a shell.

These rules run before the inline `ALLOW_DESTRUCTIVE=1` prefix is honoured.
Only an exported `HOOK_ALLOW_DESTRUCTIVE=1`, which the operator sets before
launching claude, lifts them. Every other rule keeps the prefix.

`--force-with-lease` (and `--force-if-includes`) to any other branch stays
allowed: it is the sanctioned rewrite of a pull-request branch.

## Options considered

- **Read the default branch from `origin/HEAD` with `git symbolic-ref`.**
  Rejected for now. Everything after the parse is a bash builtin, so jq or
  python3 and `cat` are this guard's whole external surface; `git` would add a
  dependency whose absence or failure needs its own fail-closed path, and the
  hook would run git config from the repository under work. `main` and
  `master` cover every repository this harness serves. A repository with
  another default branch is a recorded residual.
- **Refuse every bare `git push`.** Rejected. It is the commonest push in a
  pull-request workflow, and the branch is cheaply readable from `HEAD`.
- **Keep the regex and widen it.** Rejected. A destination check needs the
  operands in order (remote, then refspecs, with `--delete` changing what they
  mean), which a regex over the whole string cannot give without matching
  text from the next command.
- **Keep the inline prefix for push rules.** Rejected: the model can type it,
  so it would leave the rule instruction-only.
- **Leave the default branch to the server-side ruleset.** Rejected as the
  only layer. The ruleset stays the boundary; this hook stops the attempt
  earlier, in sessions and on repositories where no ruleset applies.

## Consequences

- `git push --force-with-lease origin main` now blocks. The base-expectation
  CI step goes red on that one case, by design.
- Measured on the workstation, 100 runs each: about 5.1 ms per call before, and
  6.7–7.2 ms after, for both an ordinary command and a push. About 2 ms is
  added to every Bash call.
- A here-document or commit message that contains a line beginning
  `git push origin main` blocks, as quoted `rm -rf` already did. It is the
  same known limitation (matching does not tell code from data); write such
  text with a file-writing tool.

### Residuals a text hook cannot see

- git aliases (`git p`), a wrapper script or Makefile target that pushes, and
  any interpreter that runs git through its own process API (`python -c` with
  `subprocess`).
- `remote.<name>.push` configuration or `push.default=upstream` that maps a
  feature branch onto the default branch for a bare push. The guard reads
  the current branch's name, not the push mapping.
- A default branch named other than `main` or `master`.
- The GitHub API routes onto the default branch (`git/refs` PATCH, `merges`,
  `contents` PUT). Those belong to a guard on `gh`, not to guard-bash.
- A hook that times out does not block. The push rules make no network call
  and run no external command beyond the parse (a directory is resolved in a
  subshell with builtins), so their run time stays far below the timeout.
