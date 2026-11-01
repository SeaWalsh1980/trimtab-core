# 0053. CI runs only when a PR is ready

## Status

Accepted on merge. A security control (SEC-7): the operator authorised the
changes to `.github/workflows/ci.yml` and
`.github/workflows/claude-code-review.yml` in chat, and set the rule that
`lint-test`, then `claude-review`, is never skipped. Reverses, for this repository, the instance's
standing text that CI still runs on drafts; that text and the instance's
records are not edited here.

## Context

The operator asked that CI run only when a PR is ready, and that returning a
PR to draft cancel its runs. PRs open as drafts and are pushed to until final,
so most pushes went through `lint-test` on heads nobody was yet asking to
merge. `ci.yml` left out `ready_for_review` because that event re-tested a
commit already tested.

The constraints:

- A trigger can filter on types, branches and paths, never on draft. Only a
  job-level condition can skip a draft.
- A skipped job satisfies a required check of its name, and a job's condition
  reads the event's payload, not the PR's live state. Trimtab ADR 0015 rejected
  a skip on this check for that reason: a body edit on a red PR would turn it
  green. A review of the first version of this change found the same routes
  here: re-running a draft-era run could put a skip over a red `lint-test` on a
  ready PR, and a lost or late event could leave a ready PR on a skip.
- The operator's rule: `lint-test` and `claude-review` are only ever Expected,
  in progress, failed or passed. Neither is skipped.
- Cancelling takes a new run in the same concurrency group, so
  `converted_to_draft` has to be a trigger even though that run does no work.
- The ruleset requires `lint-test`, `claude-review`, `plan-scope`,
  `merge-authority` and GitGuardian. The last is another app's, and it runs
  whatever this repository's workflows say.
- `plan-scope` voids a head on any run that did not pass, skipped and cancelled
  included (`trimtab/mergeauthority.py`, ADR 0043, ADR 0044).

## Decision

- `ci.yml` also runs on `ready_for_review` and `converted_to_draft`. The job
  has `if: !github.event.pull_request.draft`, and its name is an expression
  that is `lint-test` only when the PR is not a draft. GitHub does not evaluate
  the name of a skipped job, so the skip lands under the expression's raw text.
  No check named `lint-test` exists on a draft's head, and the required check
  stays Expected. A run on a ready PR or a push is named `lint-test`.
- A concurrency group per PR, with `cancel-in-progress: true`, cancels the run
  in progress. A push to `main` has no PR number, so each is its own group and
  none is cancelled.
- `claude-code-review.yml` also runs on `converted_to_draft`. Its job already
  skipped drafts and its group already cancelled. Its name becomes the same kind
  of expression, so `claude-review` stays Expected on a draft too.
- `plan-scope.yml` and `merge-authority-rerun.yml` do not change. `plan-scope`
  and `merge-authority` still run on a draft.

## Options considered

| Option | Why it lost |
|---|---|
| Keep running CI on drafts | The operator asked to stop |
| Skip under the name `lint-test` | The first version of this change. A skip passes the ruleset, so a lost ready event, a late event or a re-run of a draft-era run could leave a ready PR passing with nothing run. Breaks the operator's rule |
| Skip under the name `lint-test`, and test on any re-run (`github.run_attempt > 1`) | Closes the re-run route only; a lost or late event still leaves a skip |
| Leave `opened` and `synchronize` off `ci.yml`, keep only `ready_for_review` | No draft run at all, but a push to a ready PR would test nothing |
| Run `lint-test` on a draft and fail it, so a draft's check is never a pass | Takes a runner per push, which is what the change removes, and marks every draft red |
| A reusable workflow, so a skip reports under the caller's name | Same result as a second name, with one more file and a ruleset change for the new check name |
| Gate `plan-scope` on draft too | A skipped run voids the head, so a plan PR whose last push came while it was a draft could never pass on the approver route without another commit |
| Gate only `merge-authority` on draft | The check that decides whose approval a PR needs would read as passing until the ready run registers. It cannot be cancelled on return to draft either, as a group would also cancel `plan-scope` |
| Cancel with a job calling the Actions API instead of a concurrency group | More code and an `actions: write` token, for what a group does with neither |

## Consequences

- A draft still shows a run for `ci.yml`, with a skipped job and no runner
  used, and the merge box lists that job under the expression's raw text.
  GitHub offers no way to start nothing.
- If the ready event is lost, a ready PR waits on Expected and cannot merge.
  Closing and reopening the PR starts a run.
- A PR that returns to draft after its run passed keeps that pass on the head;
  a run cancelled by the return reads cancelled, which does not pass.
- A draft still runs `merge-authority` on each push, and `plan-scope` on a
  `plan/` head.
- A broken head surfaces only at ready, the moment the billed `claude-review`
  starts, and a plan run's fix rounds are then spent on it. Local checks before
  ready are the only earlier signal.
- Going ready, to draft and ready again re-tests the same commit. Converting
  to draft and back is how a review is re-triggered, so that path now also
  costs a `lint-test` run.
- A push to a ready PR cancels the run on the previous head. Only that head's
  result is lost, and it can no longer merge.
- Other repositories keep their own workflows; each needs its own change and,
  where it runs `plan-scope`, the same reasoning.
- `tests/trimtab/test_draft_ci_workflow.py` pins the triggers, the groups, the
  condition and the name. `test_plan_scope_workflow.py` still pins that
  `plan-scope.yml` has no group.

## Trial

On trimtab-core#135, 2026-11-01, reading the check runs and `ci.yml` runs on the
head through the API.

| Head | Event | Result |
|---|---|---|
| `5029eb5` (reverted version: skip named `lint-test`) | draft opened | `ci` run `skipped`; checks `lint-test` and `claude-review` `skipped`, no runner. The operator's rule then removed this |
| `09834d8` | push while draft | `ci` run `skipped`; the only skipped job carries the raw expression as its name; no check named `lint-test`, so `gh pr checks --required` does not list it |
| `09834d8` | marked ready 10:42:16 | new `ci` run created 10:42:19; check `lint-test` `in_progress` by 10:42:23 |
| `09834d8` | back to draft 10:42:54 | new `ci` run created 10:42:57; check `lint-test` `cancelled` and the ready run `cancelled` by 10:43:16; the new run `skipped` |

Not shown: a `claude-review` in progress being cancelled, because on this PR the
action skips itself and the job had already failed (it edits its own workflow).
The group and trigger are the same as `ci.yml`'s. A re-run of a draft-era run and
out-of-order events were not trialled: the skip no longer carries the required
name, and a cancel reads `cancelled`, which does not pass. A `lint-test` pass and
a push to `main` follow when this PR is marked ready and merged.

The `claude-review` name came after this trial. Its first ready run on this PR is
what shows it reporting as `claude-review`; until then it is the `lint-test`
result applied to a second job, and a wrong name would leave the check Expected.
