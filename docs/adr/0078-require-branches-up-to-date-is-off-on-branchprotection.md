# 0078. "Require branches to be up to date" is off on `BranchProtection`

## Status

Accepted, temporary. The operator turned the setting off in the GitHub UI on 2026-11-12;
this record documents that change and changes no file or setting. Repository protection is
a SEC-7 control. It is the record that
[0074](0074-drop-empty-required-deployments-rule.md) says this repository lacked: 0074
states the setting as current state only and defers the decision to a record of its own.
Related: [0073](0073-tag-ruleset-tagprotection.md) to [0077](0077-defer-push-and-branch-deletion-rulesets.md)
(the 2026-11-13/14 settings records) and [0065](0065-the-approver-route-holds-a-head-fresh-without-strict.md)
(the approver route's freshness check, written for ControlTower's `main`). No earlier
record set strict on: it was a UI setting, and 0044 and 0065 describe it as the ruleset's
state. Neither is edited; this record is the forward link for both.

## Context

Ruleset `BranchProtection` (id 24363463, target `~DEFAULT_BRANCH`, enforcement active)
requires `lint-test`, `GitGuardian Security Checks`, `claude-review`, `plan-scope` and
`merge-authority`. Read live with `gh api repos/SeaWalsh1980/trimtab-core/rulesets/24363463`
on 2026-11-14: `strict_required_status_checks_policy` is `false`.

With strict on, a PR can merge only when its head contains the current tip of `main`. Several
PRs are usually open at once. Each merge made every other open PR out of date, so each had
to be updated, and each update re-ran CI. The operator's reason, in their words: it was
hampering progress with multiple PRs open at once, and the updates and re-runs wasted GitHub
Actions minutes. No measurement drove the decision; the cost is a cost in process and
minutes.

The repository is public and owned by a personal account (`isInOrganization: false`) on
GitHub Pro.

## Decision

Leave the ruleset's required-status-checks `strict` policy off. The operator intends to turn
it back on once fewer changes are in flight. That is the re-enable condition, and it is the
operator's call: no session turns it on, and nothing here is triggered by a count of open PRs.
When it is turned back on, a new record supersedes this one; this one is not rewritten.

## Options considered

| Option | Why it lost |
|---|---|
| Keep strict on | Correct, and it is the only option with an up-to-date guarantee on GitHub's side. Rejected for cost: every merge forces an update and a CI re-run on every other open PR. |
| A merge queue | It would give the same guarantee without manual updates: the queue tests each PR on the base plus the PRs ahead. Not available here: GitHub documents merge queues for public repositories owned by an organization, or private ones on Enterprise Cloud, and not for a repository owned by a personal account. Read from GitHub's documentation, not tried: this agent cannot enable it, and the ruleset offers no merge-queue rule to check. Would be reconsidered after the Team-organization move planned in [0077](0077-defer-push-and-branch-deletion-rulesets.md), and would then need a `merge_group` trigger on the required workflows, which is a SEC-7 workflow change of its own. Not chosen now. |
| Keep strict off and add a freshness check in core | Done for the approver route only ([0065](0065-the-approver-route-holds-a-head-fresh-without-strict.md)). Extending it to every merge is "strict again, enforced by core" and re-runs CI too. |

## Consequences

Accepted negatives:

- A PR can merge on a stale base. If two PRs each pass CI alone and clash in meaning (no
  textual conflict), CI on `main` after the merge is the first to see it.
- The window between a gate's read of `main` and the merge call stays open on GitHub's side
  (the merge API pins the head, not the base).
- `lint-test`, `claude-review` and the rest are judged against the base the PR last ran on,
  not today's `main`.

What relied on strict (found by searching `docs/adr/`, `trimtab/`, `tests/`, `.github/`;
none changed here):

- [0044](0044-merge-authority-job-runs-after-plan-scope-in-one-workflow.md): relied on strict to bring
  plan-scope's base and merge-authority's base together before a merge. For the approver
  route, [0065](0065-the-approver-route-holds-a-head-fresh-without-strict.md) replaced it with
  `check-approver-route`'s freshness read. 0065 states it for ControlTower; the code reads
  freshness on every ruleset, so it holds here too. The admin route has no such check and
  judges on the pass as it stands.
- [0057](0057-merge-authority-closes-the-approver-route-on-server-facts.md): the approver's
  containment once asked for strict; `containment_problems` in `trimtab/mergeauthority.py`
  no longer does (0065).
- `.github/workflows/plan-scope.yml` (lines 18-27): its comment says plan-scope is not re-run
  when `main` moves and that strict closes that gap. With strict off, that gap is covered
  only by the 0065 freshness read, and so only on the approver route. A SEC-7 file: not edited.
- `merge-authority-rerun.yml` and 0044's wait on plan-scope: a plan-scope pass judged against an
  older base can stand on a PR whose head has not taken newer `main`. On the approver route the
  0065 freshness read refuses it; the admin route does not read it.
- [0063](0063-the-plan-run-enforcement-gate.md) §5a: a plan run resolves a conflict with `main`
  by merging it in, and that push counts as a CI fix round. Under strict, every PR behind `main`
  would have needed that, not just conflicting ones.
- `tests/trimtab/test_mergeauthority.py` runs the approver route against both `strict=False` and
  a strict ruleset (`test_an_up_to_date_head_acts_with_strict_on`), so turning strict back on
  needs no code change.

Re-enabling: turning strict on again makes the 0065 freshness read redundant on the approver
route (an up-to-date head passes it trivially) and restores the guarantee for every other
check. That change is the operator's, and its record supersedes this one.
