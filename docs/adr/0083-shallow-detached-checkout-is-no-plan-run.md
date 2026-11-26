# 0083. A shallow or detached checkout is no plan run for guard-github

## Status

Proposed (2026-11-26). Accepted when the PR that adds it merges. Closes issue 190. Narrows one
residual of ADR 0063 (§3, "A partial clone, a shallow clone or one with alternates counts as a run
for guard-github"); nothing else in that record changes, and no row of the enforcement manifest
changes.

## Context

Issue 190: in an interactive session, guard-github refused every GitHub command with E21 ("the
enforcement gate has not passed") from a shallow, detached checkout, such as the one CI makes.

The two guards read the session's mode differently. `hooks/session-mode.sh` (`sm_classify`) reads
the branch with a plain `symbolic-ref` and says interactive, since none of its signals holds.
`PlanSupport.in_run` read the branch through `tools.Git`, whose `check` refuses a shallow clone,
alternates and a partial clone. The read raised, the branch came back unknown, and `_on_plan_branch`
failed closed to "run" because git failed inside a repository (ADR 0063 round 2). E21 then applied
to a session that was not a run.

The instance's ADR 0027 row E21 says that in a run session guard-paths refuses every file write and
guard-github refuses every GitHub write and every push while the gate fails. It says nothing of
sessions that are not runs. ADR 0063 §3 added that guard-github refuses every command the prefilter
sends it, so it needs no read-versus-write reading of its own. That stays, and it is why E21 must not
be applied outside its scope.

The fail-closed rule exists so that a session cannot make itself interactive to one guard by breaking
a git read. That is a threat when the broken read changes the answer. For the branch it need not: the
name of the branch checked out names no object, and the classifier already reads it unchecked.

## Decision

A shallow or detached checkout is no run by itself. `PlanSupport.in_run` reads the checked-out branch
with `Git.symbolic_head` (`symbolic-ref --short -q HEAD` under the existing pins and environment,
without `check`), when no git is injected:

- exit 0: the branch name. A run exactly when it begins `plan/` (a `heads/` prefix from a same-named
  tag is removed, as `head_branch` does);
- exit 1: detached HEAD, no run by itself, as the classifier says;
- any other exit, a timeout or no git: `ToolError`, and `in_run` still counts a failed read inside a
  repository as a run.

With a git injected (the tests' fakes), `head_branch` answers as before. The run flag and the
recorded-session signal are untouched. guard-github's behaviour in run sessions is untouched: E21
still refuses every command it is sent while the gate fails, and the manifest does not change.

## Options considered

- **Match the classifier fully on a fatal read** (a git that fails inside a repository is
  interactive). Rejected: a session could break the read (a corrupt `.git`, a planted config the pins
  do not reach) and be interactive to guard-github alone, then mark its own PR ready by GraphQL. The
  round-2 hole returns. Only the shallow, detached case needs to agree with the classifier, and exit 1
  separates it from a failure.
- **Refuse only writes in a run, with a read allowlist.** Rejected: a read allowlist has no
  end-to-end effect, because guard-paths still refuses every Bash command in a run while the gate
  fails (ADR 0063 §3), so a run could not issue the read anyway. It also needs a decision on what is
  a read, which ADR 0063 chose not to make. Whether a run may read GitHub while the gate fails is a
  separate question, opened as its own issue.
- **Parse GraphQL to tell queries from mutations.** Rejected: more parser in the one guard whose
  refusals must be reliable, for the same absence of effect, and a mutation hidden in a variable or
  fragment would then fail open.
- **Relax guard-paths for Bash while the gate fails.** Rejected: E21 says every file write, a shell
  can write any file, and narrowing a row needs a Trimtab record (ADR 0063, options considered).

## Consequences

- An interactive session in a shallow, detached checkout (CI, a bisect) reaches GitHub as in any other
  checkout. Issue 190 is closed.
- A shallow clone on a `plan/*` branch is still a run, now by its branch rather than by the failed
  read. A clone with alternates or a partial clone is read the same way; none of the checks that
  `check` makes bears on the branch name.
- A git that fails any other way inside a repository is still a run. Strict, never looser.
- `symbolic_head` is a second git read that skips `check`. It returns a name and runs no object read;
  the pins and environment that `config_get` relies on apply to it too.
- `head_branch`, `branch_unreadable`, `push_branch` and `pr_branch` still read through the checked git
  and are unchanged. In a shallow detached checkout a bare push or a `HEAD` refspec still fails to resolve
  a branch, as before; only the run decision (E21) changed.
- The two guards still differ on spelling, in the strict direction only: guard-github strips a `heads/`
  prefix and compares without regard to case, where the classifier reads the full ref and compares
  exactly. So a branch such as `PLAN/x` is a run to guard-github and interactive to the classifier. The
  claim that a session is a run to both guards or to neither holds for the shallow and detached cases
  this record changes, not for those spellings.
- A session can make its checkout detached with `git checkout --detach`. In a full clone that already
  made it interactive to guard-github; now a shallow clone does too. The recorded-session signal
  (`_recorded`) and the run flag are the backstops, as they were for full clones. Accepted: treating a
  detached shallow clone as unknown would bring issue 190 back.
- The guard-paths review recorder (`branch_unreadable`) still reads through the checked git and so still
  refuses a review skill in a shallow detached checkout. Not changed here; it is the follow-up issue.
- The change reaches installs only when the instance moves its pin to a base with it.
- Accepted: a run cannot read GitHub while the gate fails, as before; that is the follow-up issue.
