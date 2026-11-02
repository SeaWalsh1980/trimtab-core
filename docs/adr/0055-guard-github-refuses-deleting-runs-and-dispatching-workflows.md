# 0055. guard-github refuses deleting workflow runs and dispatching workflows

## Status

Accepted on merge. The guard matrix exercises the decision. It adds refusals to
guard-github (ADR 0037) and changes none of its existing ones.

## Context

An agent account with the write role can do two things through the Actions API
that its own guards didn't read.

**It can delete a workflow run or its logs.** GitHub's documentation says anyone
with write access can. A consuming project's trial on a scratch repository showed
why that matters: forged check runs under the GitHub Actions app were refused
beside a real red, cancelled or in-progress run, and merged once the owner's
account had deleted the real red run. Deleting a run also removes what a merge
check reads about that head. Whether the agent's own account can delete one was
not tried, because the agent doesn't permanently delete data.

**It can dispatch a workflow.** That project's agent account dispatched its
production deploy workflow successfully, on 2026-08-23 and 2026-09-09. Its
`production` environment has a branch policy and no required reviewer, since a
reviewer would stop the automatic deploy. Its deploy workflow takes a commit as an
input, so a dispatch can act as a rollback. The project's owner decided on
2026-11-02 that automation only rolls forward, fixing problems with new PRs, and
that rollback stays the owner's.

## Decision

guard-github also refuses, in the spellings it already reads:

- deleting a run or its logs: `gh run delete`, and any write through the API to
  `repos/<owner>/<repo>/actions/runs/<id>` or `…/<id>/logs`;
- dispatching a workflow: `gh workflow run`, and any write through the API to
  `…/actions/workflows/<id>/dispatches` or `repos/<owner>/<repo>/dispatches`.

The API routes refuse any method but GET and HEAD, so a method the shell builds
refuses too. A shell expansion as the `gh run` or `gh workflow` subcommand refuses.
Review runs are covered like any other run.

Reads stay open, including a run's logs. So do `gh run rerun`, `gh run cancel` and
their API routes.

Reviewing that change found spellings that passed, some of them on ADR 0037's rules
as well. These apply to every rule in the guard:

- **An expansion in the repository's slots can hold slashes.** A write to
  `repos/$P/123`, `repos/$P/logs` or `repos/$A/$B` refuses, because the route may be
  in the variable. A named route after the repository stays open:
  `repos/$OWNER/$REPO/issues/1/comments`, `repos/$REPO/labels`.
- **The numeric route is read as the named one.** `repositories/<id>/…` is judged as
  `repos/<id>/<id>/…`. GitHub serves the Actions routes this way on a read. A write
  by this route wasn't tried.
- **A command word the shell builds is read past a repository option:**
  `${G}h -R o/r run delete 1`, `${G}h pr -R o/r create`. The prefilter reads past
  options between a group and its command.
- **An alias that stops at a group or at `api` refuses** (`gh alias set r run`),
  because the guard never sees the rest of what it runs. So does one to `pr review` or
  `repo sync`, which a later call could give `--approve` or `--force`, and an expansion
  that starts with an option.
- **GitHub resolves `..` in a path,** plain and percent-encoded. A read of
  `repos/<owner>/<repo>/labels/../actions/runs` returned the runs. So a delete or a
  create that ends in an expansion refuses (`labels/$X`, `issues/$X`, `labels/$X/logs`).
  A write by this route wasn't tried. A PATCH or PUT stays open, as does a named route
  after the id (`issues/$N/comments`).
- **A long payload skips the prefilter.** Its regex took over 20 s on a crafted
  140 KB command, 3.5 s before these options were read, and a hook that outlives its
  10-second timeout fails open. Above 32 KB the payload goes to the adapter, which stops
  itself and blocks. The review of this change found that; the matrix pins it.
- **That stop is 6 s, and `timeout` kills at 7 s.** They were 7 s and 8 s until the
  matrix's `under 8 s` failed at random on the merge of this change and on #144. The
  700 KB case reaches the stop and took 7.26 s on all six CI runs read. `$SECONDS`
  counts whole seconds, so it read 8 when a run began late in a second: about one run
  in four. The guard blocked every time. Loosening only the test was rejected: the base
  expectations run the base branch's copy on a PR, so that PR could still fail.

**A built command word, and `run`.** `run` is a far commoner second word than `pr`,
so a built word before `run` or `workflow` counts as gh only when a repository
option before it, or the refused subcommand after it, shows it. `$NPM run $SCRIPT`
passes.

## Options considered

| Option | Result | Why |
|---|---|---|
| Refuse in guard-github | Chosen | It's where the operator's other GitHub writes are refused, and a project's own workflow condition can back it on the server |
| Refuse re-runs too | Rejected | Sessions re-run CI legitimately: a project's `lint-test` needs a re-run after a PR body fix, since an edit starts no run. A project guards its deploy workflow's re-runs with a condition on the triggering actor, which the hook can't express |
| Refuse dispatch only for named deploy workflows | Rejected | The base can't know a project's workflow names, and a dispatch by workflow id hides the name |
| Read every built word before `run` as gh | Rejected | It refused `$NPM run $SCRIPT` and `uv run $TEST`, and a session told not to look for another route stops there, so an unattended run would block on a build command |
| Refuse cancelling runs and disabling workflows | Not done | Neither was asked for. A cancelled required run blocks a merge, which fails safe, but `gh workflow disable` stays open and can stop a scheduled scan; a later change can add it |
| Rely on a server control | Not available | The write role can do both, and no setting found separates them from the rest of `repo` on a classic token |

## Consequences

- **It's client-side.** It doesn't bind a session without the guards or a workflow
  job. A project can limit dispatch and re-run on the server with a workflow
  condition on `github.triggering_actor`. **Deleting a run has no server backstop:**
  no workflow condition can stop it, and no setting was found.
- **Residuals, which pass.** Each needs a deliberately built command:
  - a built word and a built subcommand, `${G}h run $S 1`, because that reads the same
    as `$NPM run $SCRIPT`. `${G}h pr $S` also still passes, at the prefilter, as it did
    before this change;
  - an alias whose expansion the guard can't split on spaces. A shell alias with no
    `"$@"` passes too, and it takes no arguments as far as we know, so `!gh run` runs
    only that; `!gh run "$@"` refuses;
  - `gh alias set mk 'pr create --draft'`, which the guard allows by an earlier
    decision, and which a later `gh mk --draft=false` would undo;
  - a PATCH or PUT that ends in an expansion holding `../git/refs/heads/main`, on the
    older default-branch rules, since refusing those would refuse `pulls/$N` updates;
  - an option `gh run` doesn't take, on a built word: `${G}h run -R o/r --json x
    delete 1`. As far as we know gh rejects it, and that wasn't tried.
- **Left open, by choice:** deleting an artifact or a cache. Neither is what a merge
  check reads today, and the owner asked for runs and logs. A check that comes to rely
  on an artifact needs this reopened.
- **Accepted false refusals:** `$NPM run delete`, `$NPM run $F delete` and
  `$TEMPORAL workflow run x` read the same as a built gh word. So does a write to
  `repos/$REPO/123`, or to `repos/$R` itself, through a variable, and a delete that
  ends in a variable, such as `issues/comments/$ID`: write the id literally.
- **The operator loses the same commands in a guarded session,** and lifts the
  guard with `HOOK_ALLOW_GITHUB=1`, or runs them outside it.
- **A runbook step that dispatches a scan or a CI run** is now the operator's.
- **Words in a command can reach the adapter more often.** The prefilter now passes
  a command naming `run delete` or `workflow run`. The adapter still reads a commit
  message that names them as text, and the matrix pins that.
