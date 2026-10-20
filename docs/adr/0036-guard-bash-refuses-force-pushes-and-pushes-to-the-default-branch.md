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
- `.*` ran on past the push into the next command, so `git push origin feat
  && npm ci -f` was refused as a force push.

A review of the first draft of this change then found that a parser which
trusts the text it sees is bypassed by text git or the shell reinterprets:
long-option abbreviations (`--mirr`, `--al`), refspecs the shell expands
(`$B`, `$(…)`, globs, braces), a backslash-newline, wrapper options that take
values (`nice -n 5 git`), git's guessing of a destination's namespace
(`feat:heads/main`), an operand that overrides `--repo`, and inline `-c`
configuration. Each was confirmed against git 2.47 and is now covered below.

## Decision

guard-bash parses each simple command: the command (backslash-newlines
joined) split on `; & | ( )` and backquotes, with each separator kept so the
walk knows what joined two commands; a redirection carrying `&` (`2>&1`,
`&>file`, `|&`) is folded to its plain form first, so it is not read as a
background job. Assignments (`NAME=`, `NAME+=`, `NAME[i]=`, all of which bash
takes as a prefix) and shell keywords are skipped; after `sudo`, `env`, `exec`, `nice`, `ionice`, `stdbuf`, `xargs`,
`timeout` or `time` the command word is the first token named git, wherever
it is, so a wrapper option's value cannot hide it. The code of `bash -c` /
`sh -c` is parsed as a command of its own.

For a `git … push`, after git's global options (`-C` resolved, `~` expanded;
`--git-dir`, `--work-tree` and a wrapper's `-C` / `--chdir` making the
repository unknown; `--attr-source` and the other value options skipped), it
refuses:

- any force spelling: `--force` (beside `--force-with-lease` too), a short
  cluster with `f` before any `o` (what follows `o` is the push option's
  value, so `-ofast` is not a force), a `+refspec`, `--mirror`, `--prune`;
- `--all` / `--branches`;
- each of those long options by any prefix git would accept (`--mirr`,
  `--al`, `--pru`, `--forc`, `--del`); a prefix git finds ambiguous is an
  error to git, so refusing it costs nothing;
- a destination that is `main` or `master`, in any case, after any `refs/`
  and `heads/` prefixes, since git guesses the namespace (`heads/main` and
  `refs/main` both land on `refs/heads/main`), including a delete (`:main`,
  `--delete`, `-d`). Redirections are removed first, as the shell would:
  `main>/dev/null` is the operand `main`;
- a refspec the shell would still expand (`$`, a backquote, `*`, `?`, `[`,
  `{`, or empty after quotes), a push whose operands run on into a backquoted
  substitution, and a push under `xargs`: the guard cannot see the ref that
  will be pushed;
- `-c` / `--config-env` naming `push.*`, `remote.*` or `alias.*`, and a
  `GIT_CONFIG*` assignment on the push (`GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`,
  `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_GLOBAL`), including one behind a
  wrapper's option values (`env -u X GIT_CONFIG_COUNT=…`): configuration can map a bare
  push onto the default branch, force it, or rename `push`, and when it is
  typed in the command it is the model's to choose;
- `HEAD` / `@` as the destination, or no refspec at all, when the current
  branch is `main` or `master`. The first operand is always the repository,
  even beside `--repo`. The current branch is read from the repository's
  `HEAD` file (following a linked worktree's `.git` file) in the directory the
  payload's `cwd` names, moved by `cd` and `git -C`;
- that same case when the branch cannot be read: no repository, an unreadable
  `HEAD`, `cd -`, a `cd` through a variable, `--git-dir`, a `GIT_DIR`,
  `GIT_WORK_TREE` or `GIT_COMMON_DIR` assignment on the push, `popd`, or a `cd`
  that does not move this shell (inside `( … )`, piped, followed by `||` or
  a background `&`). "Cannot tell" is not "safe" (ADR 0001).

A whole-string backstop also refuses `git push` followed by a bare `--force` or
an `f` cluster before the next separator or line, for spellings the segment
parser cannot reach. It replaces the old regex, which is removed.

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
  hook would run git configuration from the repository under work. `main` and
  `master` cover every repository this harness serves. A repository with
  another default branch is a recorded residual.
- **Refuse every bare `git push`.** Rejected. It is the commonest push in a
  pull-request workflow, and the branch is cheaply readable from `HEAD`.
- **Keep the regex and widen it.** Rejected. A destination check needs the
  operands in order (remote, then refspecs, with `--delete` changing what they
  mean), which a regex over the whole string cannot give without matching
  text from the next command.
- **Refuse any unknown long option outright.** Rejected in favour of prefix
  matching against the dangerous names: it would refuse every git option not
  listed here, and a new safe git option would block until listed.
- **After any wrapper, look for git anywhere in the segment.** Narrowed to the
  wrappers that take option values. After a shell keyword (`then`, `do`), it
  would read `then echo git push origin main` as a push.
- **Keep the inline prefix for push rules.** Rejected: the model can type it,
  so it would leave the rule instruction-only.
- **Leave the default branch to the server-side ruleset.** Rejected as the
  only layer. The ruleset stays the boundary; this hook stops the attempt
  earlier, in sessions and on repositories where no ruleset applies.

## Consequences

- `git push --force-with-lease origin main` now blocks. The base-expectation
  CI step goes red on that one case, by design.
- Two refusals of the old regex go away: a later command's `-f` after a push
  (`&& npm ci -f`, a next-line `sed -f`) is no longer read as the push's.
- Measured on the workstation, 100 runs each: about 5.4–6.4 ms per call before,
  and about 6.9–7.0 ms after, for both an ordinary command and a push: about
  1 ms added to every Bash call.
- A here-document or commit message containing a line that begins
  `git push origin main` blocks, as quoted `rm -rf` already did. It is the
  same known limitation (matching does not tell code from data); write such
  text with a file-writing tool.
- Some legitimate commands now block because their branch cannot be read:
  `(cd repo && git push)`, `cd dir || exit; git push`, a push in a backquoted
  substitution, a bare push from a directory that is not yet a repository.
  Each has a plain spelling that passes (`git -C repo push`, `cd repo && git
  push`).

### Residuals

What a text hook cannot see:

- git aliases defined in configuration (`git p`), a wrapper script or Makefile
  target that pushes, and any interpreter that runs git through its own
  process API (`python -c` with `subprocess`).
- Persisted configuration: `remote.<name>.push`, `remote.<name>.mirror`, or
  `push.default=upstream` / `matching` in a git config file, which can map a
  bare push from a feature branch onto the default branch. The guard reads
  the current branch's name, not the push mapping. (The same keys given
  inline with `-c` or a `GIT_CONFIG*` assignment are refused.)
- State set by an earlier command: `git config push.default matching`, or
  `export GIT_DIR=…` / `export GIT_CONFIG_COUNT=…`, then a plain `git push`
  in a later command or a later call. The guard judges each command on what
  it says, not on what the shell or the repository was told before.
- A shell function or alias named `git`, and a `git` planted earlier on PATH.
- `find … -exec git push` and other runners that pass operands at run time
  without being named here.

What it sees but deliberately does not cover:

- A default branch named other than `main` or `master`.
- The GitHub API routes onto the default branch (`git/refs` PATCH, `merges`,
  `contents` PUT). Those belong to a guard on `gh`, not to guard-bash.

A hook that times out does not block. The push rules make no network call and
run no external command beyond the parse (a directory is resolved in a
subshell with builtins), so their run time stays far below the timeout.
