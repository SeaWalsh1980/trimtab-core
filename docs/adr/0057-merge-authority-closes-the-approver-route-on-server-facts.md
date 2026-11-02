# 0057. Merge authority closes the approver route on server facts

## Status

Accepted on merge. Adds to ADR 0043, whose routes and admin-only paths stand. This
record covers the approval guards and the job-record guard. The merge guards and the
`check-approver-route` command follow in their own PRs and will amend it.

## Context

ADR 0043 lets the approver's approval pass a plan item PR. While a person holds the
approver account, that person is the containment. An automated approver has none:
it approves whatever the facts in front of it allow, so those facts must be ones the
agent cannot write. The agent holds the write role, so it can post commit statuses,
author a commit under any name and push to its branch. A workflow on its branch can
also forge check runs under the Actions App's id.

The admin's approval must not change. A guard that can fail the admin's own route
could stop the only person who can fix the guard. The one exception is a
configuration error, under Options.

## Decision

When the approver's approval is the one the PR would pass on (it approves the head,
the admin's approval does not, and the PR is otherwise eligible for the approver),
the approver route must be open. Otherwise the check fails and says why.

| Guard | Refuses when | Read from |
|---|---|---|
| Who pushed | Anyone but the agent moved the head branch, including a deleted account | `activity` actor |
| How it moved | Any row after the first is not a `push` (a force push, a deletion) | `activity` type |
| Unbroken | The rows don't start at the branch's creation, a row's `before` isn't the last `after`, or the newest isn't the head | `activity` before and after |
| Author | The agent did not open the PR | `pulls/N` |
| Required checks | A required check has no run from its bound App, or its latest run did not pass | ruleset `integration_id`, check run `app.id` |
| Job record | A required check bound to GitHub Actions: its latest passing run is not a finished, passing job of that name on the head, with a runner set and steps above 0, from the workflow expected for it and the `pull_request` event | `actions/jobs/<check run id>`, `actions/runs/<run id>` |
| Statuses | A commit status carries a required check's name | `commits/H/statuses` |
| Containment | A ruleset setting the approver leans on is off, a required check has no bound App, `plan-scope` and `merge-authority` aren't required from GitHub Actions, or no file path rule locks `.github/**/*` | `rules/branches/<default>` |
| Dismissal | The admin's last review is a dismissal | reviews |
| Checker in the tree | The repository runs its checker from its own tree: closed, with nothing read | `bin/trimtab` in the base commit |
| Configuration | No agent login is given (closed); the agent shares a login with the admin or approver (an error) | `--agent` |

A read that fails, is malformed or passes its bound exits 2, nothing vouched for.

These facts are read only when the PR rests on the approver. So a failed read can't
stop the admin's approval counting, and a repository with no approver makes no new
calls.

`--agent` is optional on the command line and an absent one closes the route. A
repository that sets the approver before it passes `--agent` loses the approver's
approvals, and keeps the admin's, until it does. Its job also needs `statuses: read`
and `actions: read`, or the route exits 2.

`--job-workflow CHECK=PATH`, repeated per check, names the expected workflows. They
come from the job's own configuration, so a PR can't choose them, and a required
Actions-bound check with none closes the route. A malformed value exits 2 only when the
approver carries the PR, so it can't stop the admin's approval. The event is fixed at
`pull_request`.
A trial on a scratch repository found what each kind of run looks like:

| Field | Real job | Forged run | Job from a forging workflow |
|---|---|---|---|
| Runner id and name | Set | `null` | Set |
| Steps | Above 0 | 0 | Above 0 |
| Workflow path and event | Expected | Can borrow a real run's | Its own, unless it sits at the expected path |

A carried-over job on a partial re-run kept its runner and steps with a new job id, and
its run's event and path were unchanged, so the guard needs no exception for it.

## Options considered

- **Runner fields alone, or workflow path and event alone.** Rejected: a job from a
  forging workflow has a real runner, and a forged run can borrow a real run's path and
  event. Both together still leave the residual under Consequences.
- **Tie the run to this PR.** Rejected for now, on what could be read. A run's
  `pull_requests` was empty on a merged PR and, from memory of the documentation, may list
  every open PR with that head. `commits/H/pulls` may list only open PRs for a commit off
  the default branch, so a second PR could close after its run. Neither was tried.
- **Run a trial before choosing.** Not done first, by the owner's choice: the lock check
  below stands whatever a trial finds, and the live negatives can still stage it.
- **Have the workflows refuse a non-default base.** Rejected: a change to a security
  control for a route already blocked where the checker is pinned, and it would remove
  the failed or skipped `plan-scope` run that does the blocking.
- **Lock the checker paths where the checker is in the tree.** Rejected: a repository
  like that would need its tests and code locked too, since a `pull_request` run also
  tests the merge with its base. No such repository has an approver in view.
- **Hardcode the expected workflow paths in core.** Rejected: they are each repository's
  own. The event is fixed because a required check moving to another event should be a
  decision made here.
- **Read the job for a run that hasn't passed.** Rejected: the required-checks guard has
  already refused it, so the read would add cost and nothing else.
- **Read the commit's author or committer.** Rejected: the agent writes both. A test
  pins that a commit authored as the admin earns nothing.
- **Hold the required checks to `plan-scope`'s rule, every run must pass.** Rejected
  for the others: a draft's red `lint-test` before the ready run's green is normal on
  one head. `plan-scope` keeps its stricter rule. "Latest" is by id, not start time:
  a carried-over job keeps its original `started_at`.
- **Require this check of itself.** Rejected: the run being judged cannot vouch for
  its own slot. The status guard still covers the slot, and the App's own step before
  it acts is the place to read the check's result.
- **Page the activity list.** Rejected: the endpoint pages by cursor and `GhApi` by
  number. A plan item with 100 or more ref events is odd enough to give to the admin,
  so a full page exits 2.
- **Treat activity that stops short of the creation as fine.** Rejected: older rows
  can age out, and an older push could be anyone's. A gap closes the route.
- **Close only the approver route when the agent shares a login with the admin or
  approver.** Rejected: the job can't tell which login is wrong, so it can't judge the
  admin's route either, and approver equal to admin is already an error. It exits 2 on
  every PR until the admin corrects the variable, which needs no PR.
- **Tell a stale dismissal from a person's.** Rejected: the review record doesn't say
  who dismissed it or why, and a push dismisses the admin's approval the same way a
  dismissal after a change request does. Both close the route, so the admin judges.

## Consequences

- **The job record rests on the workflow lock.** A `pull_request` run takes its workflow
  from the merge of head and base, and nothing ties it to this PR. So an agent that could
  place a workflow at the expected path on another branch, and open a second PR from the
  same head into it, could produce a run that passes every field. This comes from
  documented behaviour and hasn't been tried. Only a repository whose rules lock
  `.github/**/*` is open to the approver, so the route closes while that lock is gone. Bypass
  of that lock isn't readable here. The read is of the moment too: a branch holding a
  workflow edit from before the lock, or from a lapse, isn't covered.
- **The lock doesn't reach what a locked workflow runs.** A second PR into a branch with
  weakened tests gets a real `lint-test` green from the real workflow, with every field
  right. What stops it is `plan-scope`: on the second PR it fails from a `plan/` head, since
  only the default branch can scope it, and is skipped from any other. Either run voids the
  head under ADR 0043's every-run rule, and a test pins that. This read of the code hasn't
  been tried live. It holds only where the checker is pinned, which is why a checker in the
  tree closes the route.
- **That rule needs failed runs to survive.** Deleting the second PR's `plan-scope` run
  would lift the block while its green `lint-test` stays. A forged-run trial saw a merge
  go through after a real run was deleted, so this is likely to work, and only the
  `guard-github` hook stands against it (ADR 0055), so a route outside the hook isn't stopped.
- Anyone arming an approver must name these three residuals.
- That a forger can't set a job's runner fields rests on one forged POST that tried
  them and was ignored; it didn't try `steps` or `labels`. A green `plan-scope` on a real
  plan head has never been read, so that check's job record is untested on real data.
- This check's own slot isn't read for its job, since the run being judged can't vouch
  for itself. The approver's own step must read it before resolving or merging.
- `check-merge-authority` still exits 2 on every PR for a login clash, the one
  configuration error that can stop the admin's approval.
- The guards are conservative on purpose, and may surprise someone later. A status with
  a required check's name closes the route even if it is superseded, and any other
  account's move of the branch closes it, `github-actions[bot]` included.
- The App id for GitHub Actions is written in the code, as it is the same across
  github.com. GitHub Enterprise Server would differ; none is in use.
- A branch deleted and recreated, or rebased, closes the route for good on that
  branch. A new branch and PR, or the admin's approval, is the way on.
- An admin's approval made stale by a push closes the route on that PR, so a PR the
  admin once approved stays with the admin.
- Each guard's test was seen red on its assertion before the guard existed, except
  two controls and the pin on the commit's author, which pass either way by design.
  A mutation trial broke 73 lines one at a time: each guard's condition, the
  ordering by id, combining rules, paging the rules, each field of the job record,
  and the rule that nothing is read for the admin. A test failed for each. The first
  run left one green, a missing facts object read as open, which now has a test.
- The reads ran against a real merged PR with the agent's own token: about 4 seconds
  for all of them, every shape as the tests fake it, a real `lint-test` job passing its
  record, and the expected refusals (the owner's deletion of the merged branch, and a
  skipped `plan-scope` on a non-plan head). `GITHUB_TOKEN`'s access to `activity`,
  `statuses` and the job records is not yet shown. A 403 exits 2, which closes the route
  and leaves the admin's approval alone.
