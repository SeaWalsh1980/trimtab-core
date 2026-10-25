# 0043. The merge-authority check decides whose approval a PR needs

## Status

Accepted on merge. Builds on ADR 0041
(`docs/adr/0041-session-modes-and-plan-scoped-writes.md`) and ADR 0042
(`docs/adr/0042-plan-scope-check-judges-the-pr-diff.md`); neither is edited.
This change adds the checker only. The workflow job that runs it changes a
security control and lands in its own PR in each repository (SEC-7). Making
it a required check is the operator's ruleset change. Code-owner review
stays required; narrowing CODEOWNERS to the admin-only paths is a further
control change, in its own SEC-7 PR in each repository.

## Context

The ruleset requires code-owner review, and the admin owns every path, so
every PR needs the admin. A plan run's item PRs are meant to be approved and
merged by the approver account: the admin already authorised the plan by
merging it, and the plan-scope check confines each PR to the plan's scope.
CODEOWNERS works by path, not by the branch a PR comes from, so it cannot
let the approver approve plan PRs and nothing else.

## Decision

`trimtab check-merge-authority --repository --pr --base --head --head-ref
--head-repo --base-ref --default-branch --admin [--approver]`
(`trimtab/mergeauthority.py`) passes a PR only when:

- from a branch outside `plan/`: the admin approved it;
- from `plan/`: a `plan-scope` check run from the GitHub Actions app passed
  on the head commit, with none still running and none concluded otherwise
  (a re-run reuses its old event's base, so a re-run of an earlier pass must
  not outweigh a later failure; a new commit starts over), and
  the admin approved it, or the approver did when all of these hold: the
  branch is `plan/<plan>/item-NN-partK`, its head is in this repository,
  the PR targets the default branch, and it changes no admin-only path.

An admin-only path is one guard-paths' layer 1 refuses (`planscope.refused`:
the harness and controls, `docs/plans/authorised/` among them), either root
file that can pin the checker (`.claude/trimtab.json`, `instance.json`),
or, where the base commit has `bin/trimtab` (the checker runs from the
repository's own tree), `bin/`, `trimtab/` and `hooks/run_module.py`.
Changed paths come from `git diff base...head` with renames off, as
plan-scope reads them, so a deletion or a move of one counts. So a PR that
adds an authorised plan always needs the admin.

An approval is the account's last review that is not `COMMENTED` or
`PENDING`, in state `APPROVED`, with `commit_id` equal to the head. A last
review in state `DISMISSED` means no review; an earlier approval never
stands in. A last review in state `CHANGES_REQUESTED` from either account
fails the PR. This is the wrap-merged skill's code gate, so the gate at
merge and the skill's check after it agree.

The logins come from the caller, never from the PR: the job passes them from
configuration the PR cannot change. They are compared without case, as
GitHub compares logins. No admin login, or one login for both, is an error
(exit 2); no approver login means no approver review counts. Reviews and
check runs are read through `gh api` on repository-scoped REST paths, every
page, with `filter=all` for check runs; more than 1,000 of either, an
unexpected answer, or a failed call fails closed (exit 2).

## Options considered

| Option | Why it lost |
|---|---|
| Keep code-owner review and give the approver a CODEOWNERS entry | CODEOWNERS matches paths, not branches; an approver who owns a path can approve any PR touching it, plan run or not |
| Drop the review requirement and rely on the skill's `merged_by` check alone | The skill only stops the next session; nothing at merge would stop any account from merging an unapproved PR |
| Read the PR's changed files from the REST files endpoint | It lists at most 3,000 files; the job already has both commits, and git has no such limit |
| Make merge-authority the only gate and drop code-owner review | A `pull_request` job runs the PR's own copy of its workflow, so a PR that edits the workflow can make the job pass with no approval at all; only a rule GitHub enforces outside the PR can require the admin for those paths |
| Run the job from the base with `pull_request_target` and drop code-owner review | Review events have no base-run form, and a base-run job that fetches the PR's head handles untrusted input with a write-capable token; narrowed code-owner review closes the gap without either |
| The latest plan-scope run by `started_at` decides, as the skill reads it | A re-run of an earlier passing run reuses that event's base and becomes the latest, outweighing a failure judged against the current base |
| Take the plan-scope result from the job that ran it, as a workflow output | Within one workflow run that works, but a review event starts a new run; reading the check runs on the head covers both and is what the skill reads |
| Logins in a file in the base commit | Possible, and the job may still choose it; the checker takes them as arguments so where they live is the job's decision, made in its own SEC-7 PR |

## Consequences

- Two gates at merge. Code-owner review, with CODEOWNERS narrowed to the
  admin-only paths (workflows, pin files, checker code, authorised plans and
  the other layer 1 paths), requires the admin for any PR touching them,
  whatever the PR's copy of a workflow reports. merge-authority decides
  everything else. Until CODEOWNERS is narrowed the admin still owns every
  path, so every PR still needs the admin.
- The gate for most PRs becomes our own check. Its tests cover each route,
  stale, dismissed and voided reviews, every admin-only path kind, and the
  REST paging. A mutation trial broke
  nine decision lines one at a time (the head-commit match, the
  change-request veto, `filter=all`, the not-completed test, the app filter,
  the fork test, the pin files, counting dismissals, ordering by id); a test
  failed for each. Editing the job's workflow is itself a SEC-7 change.
- Any plan-scope run on the head that did not pass voids it, including a
  failure later fixed on the base (the admin widens the allowlist) or a
  cancelled run; a new commit starts over. The skill still reads the latest
  run, so the gate is the stricter of the two.
- The admin-only lists repeat the wrap-merged skill's approver file test. A
  test fails when that test's section of the skill stops naming any of them.
- GitHub refuses a review from a PR's author, so a non-plan PR the admin
  opens can never pass. That is no worse than today's code-owner review,
  which the admin's own PRs cannot meet either: the admin merges them by
  bypassing the ruleset, as now.
- Paths and plan-scope are read only for a plan head. A non-plan PR needs
  the admin whatever it changes, so a diff past plan-scope's 5,000-file
  bound cannot stop the admin's approval counting.
- GitHub cannot restrict who clicks merge. The wrap-merged skill's
  `merged_by` check (approver or admin) still stops a chain after any other
  merge.
- A check run belongs to a commit. The job must run on review events as well
  as PR events, or an approval given after the last push never re-runs it;
  and it must read plan-scope after plan-scope finishes, or it fails until
  the next event.
- Until the job runs and the operator makes it required, nothing changes:
  code-owner review still needs the admin on every PR, the safe default.
