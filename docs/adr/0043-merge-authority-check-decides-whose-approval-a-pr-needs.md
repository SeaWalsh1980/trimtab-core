# 0043. The merge-authority check decides whose approval a PR needs

## Status

Accepted on merge. Builds on ADR 0041
(`docs/adr/0041-session-modes-and-plan-scoped-writes.md`) and ADR 0042
(`docs/adr/0042-plan-scope-check-judges-the-pr-diff.md`); neither is edited.
This change adds the checker only. The workflow job that runs it changes a
security control and lands in its own PR in each repository (SEC-7). Making
it a required check, and then dropping code-owner review, is the operator's
ruleset change.

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
  on the head commit (the latest by `started_at`, none still running), and
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
| Read the PR's changed files from the REST files endpoint | It lists at most 3,000 files and reports a rename as one entry; the job already has both commits, and git has neither limit |
| Take the plan-scope result from the job that ran it, as a workflow output | Within one workflow run that works, but a review event starts a new run; reading the check runs on the head covers both and is what the skill reads |
| Logins in a file in the base commit | Possible, and the job may still choose it; the checker takes them as arguments so where they live is the job's decision, made in its own SEC-7 PR |

## Consequences

- The gate at merge becomes our own check instead of GitHub's code-owner
  rule. Its tests cover each route, stale, dismissed and voided reviews,
  every admin-only path kind, and the REST paging; a mutation run against
  the decision lines showed each one is held by a test. Editing the job's
  workflow is itself a SEC-7 change.
- GitHub cannot restrict who clicks merge. The wrap-merged skill's
  `merged_by` check (approver or admin) still stops a chain after any other
  merge.
- A check run belongs to a commit. The job must run on review events as well
  as PR events, or an approval given after the last push never re-runs it;
  and it must read plan-scope after plan-scope finishes, or it fails until
  the next event.
- Until the job runs and the operator makes it required, nothing changes:
  code-owner review still needs the admin on every PR, the safe default.
