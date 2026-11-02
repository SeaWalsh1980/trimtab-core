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
reviewer would stop the automatic deploy. A dispatch can name an older commit, so
it can act as a rollback. The project's owner decided on 2026-11-02 that automation
only rolls forward, fixing problems with new PRs, and that rollback stays the
owner's.

## Decision

guard-github also refuses, in the spellings it already reads:

- deleting a run or its logs: `gh run delete`, and any write through the API to
  `repos/<owner>/<repo>/actions/runs/<id>` or `…/<id>/logs`;
- dispatching a workflow: `gh workflow run`, and any write through the API to
  `…/actions/workflows/<id>/dispatches` or `repos/<owner>/<repo>/dispatches`.

The API routes refuse any method but GET and HEAD, so a method the shell builds
refuses too. A shell expansion as the `gh run` or `gh workflow` subcommand refuses,
and the hook's prefilter sends a command word the shell builds to the adapter when
`run delete` or `workflow run` follows it. Review runs are covered like any other
run.

Reads stay open, including a run's logs. So do `gh run rerun`, `gh run cancel` and
their API routes.

## Options considered

| Option | Result | Why |
|---|---|---|
| Refuse in guard-github | Chosen | It's where the operator's other GitHub writes are refused, and a project's own workflow condition can back it on the server |
| Refuse re-runs too | Rejected | Sessions re-run CI legitimately: a project's `lint-test` needs a re-run after a PR body fix, since an edit starts no run. A project guards its deploy workflow's re-runs with a condition on the triggering actor, which the hook can't express |
| Refuse dispatch only for named deploy workflows | Rejected | The base can't know a project's workflow names, and a job ID or a dispatch by number hides the name |
| Refuse cancelling runs and disabling workflows | Not done | Neither was asked for. A cancelled required run blocks a merge, which fails safe, but `gh workflow disable` stays open and can stop a scheduled scan; a later change can add it |
| Rely on a server control | Not available | The write role can do both, and no setting found separates them from the rest of `repo` on a classic token |

## Consequences

- **It's client-side.** It doesn't bind a session without the guards or a workflow
  job. A project that needs a limit on the server sets one in the workflow, as a
  condition on `github.triggering_actor`.
- **The operator loses the same commands in a guarded session,** and lifts the
  guard with `HOOK_ALLOW_GITHUB=1`, or runs them outside it.
- **A runbook step that dispatches a scan or a CI run** is now the operator's.
- **Words in a command can reach the adapter more often.** The prefilter now passes
  a command naming `run delete` or `workflow run`. The adapter still reads a commit
  message that names them as text, and the matrix pins that.
