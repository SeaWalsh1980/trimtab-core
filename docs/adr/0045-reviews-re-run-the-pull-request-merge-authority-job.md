# 0045. Reviews re-run the pull_request merge-authority job

## Status

Accepted on merge. Supersedes the review-event part of ADR 0044
(`docs/adr/0044-merge-authority-job-runs-after-plan-scope-in-one-workflow.md`),
which is not edited: its triggers, and its claim that the required check
takes the latest run of a name on a commit. The rest of ADR 0044 stands. A
security control (SEC-7).

## Context

ADR 0044 started `plan-scope.yml` on review events too, so an approval given
after the last push would be judged, and relied on the ruleset reading the
latest `merge-authority` run on the head. The live run on 2026-10-26
disproved that. On trimtab-core#116, with both checks required, the admin
approved, and the review-event run passed. The merge box still listed
`plan-scope / merge-authority (pull_request)` as a failing required check,
beside a passing `(pull_request_review)` entry. GitHub keeps a required check
per triggering event, and a run from one event never replaces another's.

The ruleset also requires approval of the most recent push and dismisses
stale approvals, so a valid approval always comes after the last push. As
built, no PR could pass `merge-authority` on an approval alone.

## Decision

- `plan-scope.yml` runs on `pull_request` events only, as before ADR 0044.
- A new workflow, `merge-authority-rerun.yml`, runs on `pull_request_review`
  (`submitted`, `edited`, `dismissed`). It finds plan-scope.yml's latest
  `pull_request` run on the head, waits at most 5 minutes for it to finish,
  and re-runs that run's `merge-authority` job through the REST API. The job
  reads the reviews again, and its result replaces the `pull_request` entry
  the ruleset reads.
- The re-run workflow judges nothing. Its job is named
  `merge-authority-rerun`, so its own check never counts. Its token may
  re-run jobs (`actions: write`) and read contents, nothing else.
- The merge-authority job keeps ADR 0044's other choices: refs from the
  event's PR, the base branch's tip, the bounded wait for plan-scope, the
  logins from Actions variables.

## Options considered

| Option | Why it lost |
|---|---|
| Keep review-event runs of `plan-scope.yml` (ADR 0044) | Their entry sits beside the `pull_request` one; the failed `pull_request` entry keeps blocking |
| Require an edit of the PR after approving, which starts a `pull_request` run | A manual step on every PR; easy to forget, and an `edited` run also re-judges plan-scope |
| Make the job pass on `pull_request` events and judge only on reviews | A pass before any approval would satisfy the ruleset |
| Post a commit status from the review run instead of a check run | Needs `statuses: write`, and a status can be posted by anyone with that scope, not only this app; the ruleset binds the check to GitHub Actions |
| Re-run the whole `pull_request` run | It re-runs plan-scope too: a minute more per review, and a fresh run judged against the old event's base |

## Consequences

- An approval reaches the ruleset about a minute after it is given: the
  re-run workflow starts, waits for nothing in the usual case, and the
  re-run job reads the reviews.
- A review on a PR whose `pull_request` run is still going waits up to 5
  minutes; if it is still going then, the re-run workflow fails, and the
  `pull_request` run's own result stands. A later review or a re-run from the
  Actions page decides.
- A fork's PR gets a read-only token on review events, so its re-run fails.
  It needs the admin in any case, who bypasses the ruleset (ADR 0043).
- A re-run reuses its run's event, whose base can be older than the base
  branch. The merge-authority job already checks out the base branch's tip,
  so it judges by the newest rule.
- Like every `pull_request_review` job, the re-run job runs the PR's own copy
  of its workflow with the permissions that copy asks for. A branch in this
  repository can already ask for those in any `pull_request` workflow; the
  change is not new exposure, and code-owner review on `.github/` keeps a
  changed copy from merging.
- `tests/trimtab/test_plan_scope_workflow.py` fails if `plan-scope.yml`
  gains a trigger other than `pull_request`, or if the re-run workflow's
  trigger, job name, permissions or target change.
- The fix PR proves itself. Its own review runs its copy of the re-run
  workflow, so approving it should flip its `merge-authority
  (pull_request)` entry to pass without a bypass.
