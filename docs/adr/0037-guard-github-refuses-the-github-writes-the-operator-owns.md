# 0037. guard-github refuses the GitHub writes the operator owns

## Status

Accepted on merge. No trial is pending: the guard matrix exercises the
decision in full. The change adds a guard and changes no existing guard, so
the base branch's matrix holds against it.

## Context

A consuming project's instruction file carries rules that bind an action, not
a path: open every pull request as a draft; never approve a pull request as
the agent's account; never resolve a review thread (only its author may); and
never write to the default branch. Its branch ruleset and code owners back
some of these on the server, but not all of them (a ruleset does not stop a
non-draft PR or a thread resolution), and a rule the agent must obey whatever
it decides belongs in a hook as well (the base rule that anything which must
hold is enforced by a hook, permission rule or test, not instruction text
alone). guard-bash (ADR 0036) covers `git push`; nothing covered the same
writes made through GitHub's API.

The agent reaches GitHub three ways from a shell: gh's porcelain
(`gh pr create`, `gh pr review`), `gh api` (REST and GraphQL), and a raw HTTP
client with a token (`curl -H "Authorization: bearer $(gh auth token)"`). Each
has its own spellings: pflag short-option clusters (`-dw`, `-ca`), options
given as `--opt=value`, a body from `--input <file>`, a field from
`-F key=@file`, a method in a cluster (`-iXPOST`), a full URL, a path with
`..` or a percent-encoded slash, a GraphQL name spelled with a JSON `\u`
escape, and every way the shell hides a word (quotes inside it, a backslash,
`$'…'`, a variable, `bash -c`, `eval`, a pipe into a shell, an alias).

## Decision

A new PreToolUse guard on `Bash`, `hooks/guard-github.sh`, with its logic in
`trimtab/adapters/guard_github.py`, run through `hooks/run_module.py` under
`python3 -I -S -B` (ADR 0035). It blocks (exit 2) with one line naming the
rule; the line never quotes the command.

**What it refuses.**

- A PR opened as anything but a draft: `gh pr create` (or its alias
  `gh pr new`) unless `--draft` / `-d` is set last, and always with `--web`
  (the browser form can create a non-draft PR); a REST `POST …/pulls` unless
  the body's `draft` is the boolean `true` (`-F draft=true`, or
  `"draft": true` in an `--input` file); a GraphQL `createPullRequest`
  without `draft: true` outside its strings and comments.
- An approving review: `gh pr review --approve` / `-a`, in a cluster too
  (`-ca`); a REST review or review event whose `event` is APPROVE in any case;
  a GraphQL `addPullRequestReview` / `submitPullRequestReview` whose event is
  APPROVE, in the query or in a variable.
- Resolving a review thread: a GraphQL query naming `resolveReviewThread`, in
  any case and from any source (`-f`, `-F query=@file`, an `--input` body,
  which is JSON-decoded first, so `\u` escapes are read).
- A write to the default branch through the API: a write to
  `git/refs/heads/<default>` (PATCH or DELETE), a `git/refs` create naming
  it, a `merges` POST into it (or with no base), a `contents` PUT or DELETE to
  it or with no branch (the API's default), a `merge-upstream` of it, and
  `gh repo sync --force`. GraphQL `updateRef`, `updateRefs` and `deleteRef`
  name the ref by an opaque ID, so they are refused outright;
  `createCommitOnBranch` and `mergeBranch` when they name the default branch
  or name no branch. The default branch is `main` or `master`, as in ADR 0036.
- A write to a github.com host through `curl` or `wget` (a data, form or
  upload option, or a method other than GET or HEAD), and options this guard
  cannot read: `curl -K` / `--config` (in a cluster too, `-sK`, and `-K-` from
  stdin), `wget -e` / `--execute` / `--config`. Reads pass.

**How it reads a command.** Two passes, as in ADR 0036, and either may refuse.

1. *Quote-aware.* The same regex tokeniser guard-bash uses (the matrix checks
   that the two patterns are the same text) splits the command at
   `; & | ( )`, backquotes and newlines, joins quoted pieces into words, and
   marks a word *dynamic* when the shell would expand it. Options are read the
   way pflag reads them: clusters letter by letter, a valued option taking the
   rest of the cluster or the next word, `--opt=value`, and no abbreviations
   (cobra accepts none). The last setting of an option wins. `bash -c`,
   `sh -lc`, `eval`, `env -S` and `gh alias set` re-read the text they will
   run, and so
   does a `$(…)` or backquote substitution inside double quotes; text re-read
   keeps its expansions dynamic.
2. *Naive.* Every quote and backslash removed, split the same way, every word
   literal. It runs only when the command holds a word that runs quoted text
   as a command (`bash`, `sh`, `eval`, `source`, `xargs`, `ssh`, …), so
   `echo '…' | bash` is read, while a commit message or a comment body that
   names a refused command is not. A substitution does not trigger it: the
   naive split at `$(` cut `--draft` off a create whose title came from one,
   and the quote-aware pass reads substitutions itself.

**Fails closed.** In the quote-aware pass, anything the decision needs and
cannot read refuses: an option it does not know (whether it takes a value
decides what the next word is), a dynamic word where an option may stand, a
dynamic `gh` command or subcommand, a body on stdin, an unreadable or non-JSON
`--input`, a field or path built by the shell on a write, a relative file
after a `cd`, a field given twice with different values (which one gh sends is
gh's merge rule, not this guard's to assume), an alias expansion read from
stdin, gh run by `xargs` or `parallel` (they append arguments read from stdin,
which can unset `--draft` or add `--approve`; guard-bash refuses `xargs`
pushes for the same reason, ADR 0036), and a request file whose name appears anywhere else in the command:
the hook reads the file before the command runs, so a redirect, `cp`, `tee`
or `sed -i` earlier in the same command could rewrite what gh then sends. A
path whose expansions stand only for the owner, repository or an ID
(`repos/$OWNER/$REPO/pulls/$N/reviews`) is read; one where an expansion may be
a route word is not. GraphQL is any path ending in `graphql`, so GitHub
Enterprise's `api/graphql` is read too.

**Time.** A hook that outlives its timeout fails open, so nothing the command
names may make the adapter slow. A file it reads (`--input`, `-F key=@file`)
is opened non-blocking and read only if it is a regular file of at most 1 MiB;
a FIFO, a device or a larger file is refused unread (a FIFO hung the first
draft for the full timeout, and `/dev/zero` exhausted memory). The adapter
blocks itself at 7 s, and the hook runs it under `timeout`, which kills it at
8 s; both end in a block, inside the 10 s timeout.

The hook blocks on an empty payload, one that is not a JSON object, a missing
or older python3, a missing loader, and any adapter error. A bash prefilter
lets a command through without python3 only when, with JSON whitespace escapes
turned to spaces and quotes and backslashes removed, it names neither `gh`,
`graphql`, `github.com` nor one of the gh subcommands judged here
(`pr create`, `pr review`, `api`, `repo sync`, `alias set`, `alias import`),
so a `gh` word the shell builds (`${G}h`, `/usr/bin/g?`) still reaches the
adapter; a `\u` escape or `$'` sends it there regardless.

**Override.** `HOOK_ALLOW_GITHUB=1`, exported by the operator before launching
claude. An inline prefix never reaches the hook, and the installer refuses any
`HOOK_ALLOW_*` key in a settings layer.

**Registration.** `settings.base.json` registers it once on `Bash` with the
10 s timeout and adds it to the SessionStart liveness list as a guard, and
`bootstrap.sh` probes it (a non-draft create and a garbage payload must both
block) like the other blocking guards.

## Options considered

- **A rule in the instruction file alone.** Rejected: the base rule above
  requires a mechanism for what must hold.
- **Rely on the server.** Rejected as the only layer: the ruleset and code
  owners stop an unapproved merge, but not a non-draft PR, an approval by an
  account that is not a code owner, or a resolved thread. The server stays the
  boundary; this is the second layer, and it makes the right path the easy
  one.
- **Extend guard-bash.** Rejected: guard-bash is bash with a jq or python3
  parse, tuned for one command family. Reading GraphQL bodies, JSON files and
  REST routes needs python3, and a separate guard keeps its own override and
  its own probe.
- **Extend pr-body-check, which already parses `gh api` pull writes.** Rejected:
  it is policy and fails open (ADR 0005), and it imports the registry, which
  needs PyYAML. This guard runs with `-S`, standard library only. Its parser
  also reads `-iXPOST` and other clusters, which pr-body-check's does not;
  pr-body-check is left unchanged.
- **Refuse every `gh api` write, every GraphQL mutation, or every `curl` to
  GitHub.** Rejected: replies to review comments, labels and issue comments are
  ordinary work and go through the same routes.
- **Allow an unknown option.** Rejected: whether it takes a value decides
  whether the next word is `--draft` or a title, so the decision depends on
  it. The cost is a refusal when gh gains an option before this table does.
- **Run the naive pass on every command.** Rejected after a trial: a comment
  reply whose body named `gh pr create` was refused. The pass now runs only
  where quoted text can become a command.

## Consequences

- The four action rules hold in every spelling the matrix lists, as a second
  layer behind the ruleset and code owners.
- An ordinary command costs about 2 ms (the bash prefilter alone). A command
  naming GitHub starts python3: about 48 ms, and about 0.8 s for a 135 KB
  command, against the 10 s timeout (measured on the development workstation).
- Accepted false positives, each a refusal that a file or a rewrite clears:
  `gh "$@"` and any dynamic gh subcommand; an unquoted `gh` word in another
  command (`echo gh pr create`); a REST create with `-f draft=true` (a string,
  not the boolean); a review command whose PR number is a variable in an
  option position; text that names a refused command inside a command that
  runs text (a heredoc passed to `bash`). A pull request body or a comment
  body that names one passes when it is sent with `-f body=…`, and always when
  it is sent from a file.

### Residuals

The guard reads the command text, so it does not see:

- a script, interpreter or program that calls GitHub itself (`python3 x.py`,
  `node`, a Makefile), or a script piped into a shell from a file;
- a gh alias or extension defined before the session, or in a file;
- a command word that comes from the environment rather than the command;
- an MCP tool (guard-mcp's, ADR 0013 and 0034) or the desktop app's tools;
- a default branch named other than `main` or `master`;
- a GitHub Enterprise host reached by `curl` (only github.com hosts are
  matched); gh against any host is read the same way;
- state set by an earlier command: a `~/.curlrc` or `~/.wgetrc` that adds a
  method or data, a request file written by an earlier call and read here,
  a gh alias defined earlier;
- a request file rewritten in the same command through a name the guard
  cannot match to it: a glob (`> re*.graphql`), a variable (`cp x "$F"`), or
  another link to the same file. Matching those would mean refusing every
  command that names a glob or a variable beside a file read, which refuses
  ordinary calls; it takes deliberate evasion, which the server still stops.

Each is a reason the server stays the boundary.
