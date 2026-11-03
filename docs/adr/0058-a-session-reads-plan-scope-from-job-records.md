# 0058. A session reads plan-scope from job records

## Status

Accepted on merge. Adds to ADR 0057, whose guards and whose job's reads don't change, and
departs from one option it rejected: see "The workflow path" below. Follows AGW-4 as amended
in the instance's ADR 0024, which merges first.

## Context

`wrap-merged` lets an approver's merge authorise a plan's next work only when `plan-scope`
passed on the PR's final head. It told a session to read that from
`commits/<head>/check-runs`.

An operator can give sessions a fine-grained token, to keep them away from Actions secrets
and variables. GitHub's permissions for those tokens list no Checks permission, so that read
gets a 403, as do `gh pr checks` and `statusCheckRollup`. Read on a real repository with such
a token: check runs 403; `actions/runs`, `actions/runs/<id>/jobs`, `rules/branches/<branch>`
and `rulesets/<id>` all answer. So every approver-merged record would stop a plan session,
though nothing was wrong.

## Decision

`trimtab check-plan-scope-job --repository OWNER/NAME --head SHA` answers it from job records,
and `wrap-merged` calls it.

| Case | Exit |
|---|---|
| The ruleset requires `plan-scope` bound to GitHub Actions, and every `plan-scope` job on the head is real and passing | 0 |
| The ruleset doesn't, no such job exists, a job or a run of the expected workflow is unfinished, or a job isn't real and passing | 1 |
| A read fails or returns the wrong shape, past a bound, or an argument is bad | 2 |

- **Real and passing** is `_real_job` from ADR 0057: finished, passing, with a runner and
  steps, on this head, from `.github/workflows/plan-scope.yml` and `pull_request`.
- **Every job, every attempt, every workflow.** It reads all the head's workflow runs and takes
  jobs by their exact name, so a forged job of that name beside the real one voids the head, as a
  failed attempt does. A lookalike name isn't judged: the ruleset requires the exact name too.
  This is `plan_scope_problem`'s every-run rule, not the skill's old "latest must pass", which
  was looser than the merge gate.
- **An unfinished run of the expected workflow counts.** A queued re-run may list no job for its
  new attempt yet, and the earlier attempt's pass must not answer for it.
- **The workflow path** is a constant, `.github/workflows/plan-scope.yml`. ADR 0057 rejected
  hardcoding expected paths in core because they are each repository's own. That holds for the
  job, which gets them from configuration a PR can't change. A session has no such configuration:
  whatever it passes, it chose. The cost is under Consequences.
- `check-merge-authority` and `check-approver-route` don't change. They judge checks from other
  Apps as well. The job's token can read check runs; the App's step is to be shown when it's wired.
- The skill still reads `rulesets/<id>` for `updated_at`, which needs the PR's `merged_at`.

## Options rejected

- **Skill text alone, telling a session to read the same endpoints.** Untested instruction, the
  state ADR 0056's amendment moved away from.
- **Job records inside `check-merge-authority`.** A job record can't speak for a check run from
  another App, so the required-checks guard would weaken. No skill or hook runs that command in a
  session, and there it exits 2 on the 403, which vouches for nothing.
- **A Checks-capable token for sessions.** Fine-grained tokens have no such permission, and a
  classic one reaches what the fine-grained token exists to keep from sessions. A read-only App's
  token would work, for a new App and a key on each machine, to read what job records already show.
  Not tried.
- **Reading the `merge-authority` job's result instead.** It judges approvals at that moment, not
  this test, and a PR merged by bypass may have no passing run.
- **The latest attempt only.** A re-run reuses its old event's base, so it could outweigh a later
  failure (`plan_scope_problem`).
- **Only jobs at the expected path.** A forged job elsewhere would then go unseen.
- **A `--workflow` argument.** A session would choose what judges it.

## Consequences

- **No check run is compared.** The App's id is taken from where the record is read: the Actions
  API lists only Actions jobs. The ruleset's binding is still read. A job's id is its check run's
  id (ADR 0057), so this reads the same run through another door, with a runner and steps that a
  forged check run lacks.
- **AGW-4 changes first.** It said the session verifies "a check run", and a review of this
  change raised going ahead of it as HIGH. The instance's ADR 0024 amends it to job records, on the
  operator's approval. Nothing here should merge before that.
- **Another path stops the plan.** A repository running `plan-scope` from any other path gets exit 1
  on every approver merge, though its merge gate passes, and the message says the job isn't
  a passing one from the constant's path.
- **A deleted run is invisible.** ADR 0057 reports a merge going through after a deletion, so a
  check-run read likely misses it too; that's inferred, not tried here. `guard-github` refuses
  deleting one; the agent's server rights don't.
- **A workflow run per head costs a read of its jobs.** No batch endpoint exists, and it's bounded:
  200 runs a head, 200 jobs a run. Past that it's exit 2. A session runs it once per record.
- **Still called by an instruction,** as `check-plan-admin` is. No hook makes a session run it.
- **Any non-passing job stops the plan,** including a cancelled one on a head the merge gate would
  also refuse. The admin's removal of `plan:blocked` is the way past it.

## Trial

Each test was seen failing on its assertion against a stub that reported no problem, then passing.
A mutation trial broke 35 lines one at a time, over two runs. Each run left 1 green: removing the
check on a workflow run's id, because the fake's unanswered path failed anyway, and an unfinished
run hidden by a later finished one. Each now has a test, and all 35 are caught. The tests at
exactly a bound pass either way until `>` becomes `>=`, which the trial covers.

Read-only against a real repository with a fine-grained token: a head whose `plan-scope` job was
skipped exits 1, naming it, and a head with no runs exits 1. A passing job on a real plan head is
unread: no plan head has run yet.
