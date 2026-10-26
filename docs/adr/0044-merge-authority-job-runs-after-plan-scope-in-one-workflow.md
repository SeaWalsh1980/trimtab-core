# 0044. The merge-authority job runs after plan-scope, in one workflow

## Status

Accepted on merge. Builds on ADR 0042
(`docs/adr/0042-plan-scope-check-judges-the-pr-diff.md`) and ADR 0043
(`docs/adr/0043-merge-authority-check-decides-whose-approval-a-pr-needs.md`);
neither is edited. ADR 0043 left the source of the logins and the job's
shape to this change. It is a security control (SEC-7). Making the check
required is the operator's ruleset change.

## Context

`trimtab check-merge-authority` judges a PR at one moment, from its reviews
and the plan-scope check runs on its head. GitHub keeps a check run per
commit, and an approval adds no commit, so a job run only on PR events
never sees an approval given after the last push. The checker also fails
while any plan-scope run on the head is unfinished, so it must judge after
plan-scope. The checker takes the admin and approver logins as arguments;
something the PR cannot change must supply them.

## Decision

`merge-authority` is a second job in `.github/workflows/plan-scope.yml`:

- The workflow runs on `pull_request` (as before) and on
  `pull_request_review` (`submitted`, `edited`, `dismissed`).
- Both jobs take the head and base refs from `github.event.pull_request`.
  The context's `head_ref` and `base_ref` are empty on review events, and
  plan-scope's condition read them, so it would skip there; a skipped run on
  the head counts as not passed.
- plan-scope runs for plan heads on review events too, for the same reason.
- merge-authority has `needs: plan-scope` and `if: !cancelled()`, so it runs
  whatever plan-scope concluded; the checker judges plan-scope's runs
  itself.
- Before judging it waits, at most 5 minutes, until no plan-scope run on the
  head is still going, so two overlapping runs (a push and a review moments
  apart) do not leave a spurious failure.
- It checks out the base branch's tip as it starts, not the event's base
  commit. A review event carries the base GitHub last recorded for the PR,
  which can predate a change to the admin-only paths or the checker; the tip
  is the newest rule. The checker diffs from the merge-base, so it reads the
  PR's own changes either way.
- The logins come from the repository's Actions variables `TRIMTAB_ADMIN`
  and `TRIMTAB_APPROVER`. An empty `TRIMTAB_ADMIN` is exit 2; an unset
  approver means no approver review counts.
- Its token reads contents, pull requests and checks, and nothing else.

## Options considered

| Option | Why it lost |
|---|---|
| A separate workflow for merge-authority | `needs:` works only within one workflow; a separate one would judge while plan-scope runs and fail until the next event |
| Skip plan-scope on review events (`if: event_name == 'pull_request'`) | The skipped run lands on the head, and the checker counts it as not passed, so every approver approval would fail |
| A `concurrency:` group per PR | It cancels an in-progress or pending run; a cancelled plan-scope run voids the head until the next push |
| Logins in a file in the base commit (`.claude/trimtab.json`, `instance.json`) | Both are admin-only paths, so it would be safe, but trimtab-core is public and generic; the logins are each repository's configuration, set where the operator already sets repository configuration, and no instance pin bump is needed to change them |
| Logins as Actions secrets | They are not secret, and secrets are masked in logs, which hides which login a message names |
| Check out the event's base commit, as plan-scope does | On a review event it can be older than the base; an approval could then be judged by an older admin-only list |
| `always()` instead of `!cancelled()` | A cancelled workflow would still run the job; cancelled must stay not passed |

## Consequences

- Each review on a plan PR, comments included, costs one more plan-scope run
  and one merge-authority run. A review on a merged plan PR re-judges it, as
  an `edited` event already does (ADR 0042).
- Setting the variables is Actions configuration, a control, so it is the
  operator's. Until `TRIMTAB_ADMIN` is set the check fails on every PR; the
  ruleset must not require it before then.
- GitHub may withhold variables from a run started by a fork's PR. Such a PR
  then fails with an error naming that cause. A fork's PR needs the admin
  whatever happens, and the admin merges it by bypassing the ruleset, as for
  the admin's own PRs (ADR 0043).
- The wait is bounded. If plan-scope is still running after 5 minutes, the
  checker judges and fails on it; the later run decides, since the required
  check takes the latest run of a name on a commit.
- plan-scope still judges against its event's base (ADR 0042), so the two
  jobs may use different base commits. merge-authority's is never the older,
  so the gate is the stricter. "Require branches to be up to date" brings
  the two together before a merge.
- `tests/trimtab/test_plan_scope_workflow.py` fails if the review triggers,
  the event-sourced refs, the `needs`/`!cancelled()` wiring, the read-only
  permissions, the base-branch checkout or the login source change, or if a
  `concurrency:` group appears. The checker's own tests cannot see any of
  these, and each breakage fails closed, so nothing else would flag it.
- Both jobs share the setup-python and PyYAML steps by YAML anchor, so the
  pin is written once in the workflow.
