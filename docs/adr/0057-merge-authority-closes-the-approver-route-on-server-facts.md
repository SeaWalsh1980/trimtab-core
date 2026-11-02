# 0057. Merge authority closes the approver route on server facts

## Status

Accepted on merge. Adds to ADR 0043, whose routes and admin-only paths stand. This
record covers the approval guards. The job-record guard, the merge guards and the
`check-approver-route` command follow in their own PRs and will amend it.

## Context

ADR 0043 lets the approver's approval pass a plan item PR. While a person holds the
approver account, that person is the containment. An automated approver has none:
it approves whatever the facts in front of it allow, so those facts must be ones the
agent cannot write. The agent holds the write role, so it can post commit statuses,
author a commit under any name and push to its branch. A workflow on its branch can
also forge check runs under the Actions App's id.

The admin's approval must not change. A guard that can fail the admin's own route
could stop the only person who can fix the guard.

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
| Statuses | A commit status carries a required check's name | `commits/H/statuses` |
| Containment | A ruleset setting the approver leans on is off, a required check has no bound App, or `plan-scope` and `merge-authority` aren't required from GitHub Actions | `rules/branches/<default>` |
| Dismissal | The admin's last review is a dismissal | reviews |
| Configuration | No agent login is given (closed); the agent shares a login with the admin or approver (an error) | `--agent` |

A read that fails, is malformed or passes its bound exits 2, nothing vouched for.

These facts are read only when the PR rests on the approver. So a failed read can't
stop the admin's approval counting, and a repository with no approver makes no new
calls.

`--agent` is optional on the command line and an absent one closes the route. A
repository that sets the approver before it passes `--agent` loses the approver's
approvals, and keeps the admin's, until it does. Its job also needs `statuses: read`
and `actions: read`, or the route exits 2.

## Options considered

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
- **Tell a stale dismissal from a person's.** Rejected: the review record doesn't say
  who dismissed it or why, and a push dismisses the admin's approval the same way a
  dismissal after a change request does. Both close the route, so the admin judges.

## Consequences

- A forged check run with a real run's name and App still satisfies the required
  checks guard here. The job-record guard is what refuses it, so this guard is not
  enough until that lands.
- The App id for GitHub Actions is written in the code, as it is the same across
  github.com. GitHub Enterprise Server would differ; none is in use.
- A branch deleted and recreated, or rebased, closes the route for good on that
  branch. A new branch and PR, or the admin's approval, is the way on.
- An admin's approval made stale by a push closes the route on that PR, so a PR the
  admin once approved stays with the admin.
- Each guard's test was seen red on its assertion before the guard existed, except
  two controls and the pin on the commit's author, which pass either way by design.
  A mutation trial broke 29 lines one at a time, one for each guard's condition, the
  ordering by id, and the rule that nothing is read for the admin. A test failed for
  each. The first run left one green, a missing facts object read as open, which now
  has a test.
- The reads ran against a real merged PR with the agent's own token: about 3 seconds
  for all five, every shape as the tests fake it, and the expected refusals (the
  owner's deletion of the merged branch, and a skipped `plan-scope` on a non-plan
  head). `GITHUB_TOKEN`'s access to `activity` and `statuses` is not yet shown. A 403
  exits 2, which closes the route and leaves the admin's approval alone.
