# 0053. CI runs only when a PR is ready

## Status

Accepted on merge. A security control (SEC-7): the operator authorised the
changes to `.github/workflows/ci.yml` and
`.github/workflows/claude-code-review.yml` in chat. Reverses, for this
repository, the instance's standing text that CI still runs on drafts; that
text and the instance's records are not edited here.

## Context

The operator asked that CI run only when a PR is ready, and that returning a
PR to draft cancel its runs. PRs open as drafts and are pushed to until final,
so most pushes went through `lint-test` on heads nobody was yet asking to
merge. `ci.yml` left out `ready_for_review` because that event re-tested a
commit already tested.

The constraints:

- A trigger can filter on types, branches and paths, never on draft. Only a
  job-level condition can skip a draft, and that leaves a run with a `skipped`
  job on the head.
- A `skipped` job satisfies a required check. A draft cannot merge, so the
  skip matters only once the PR is ready.
- Cancelling takes a new run in the same concurrency group, so
  `converted_to_draft` has to be a trigger even though that run does no work.
- The ruleset requires `lint-test`, `claude-review`, `plan-scope`,
  `merge-authority` and GitGuardian. The last is another app's, and it runs
  whatever this repository's workflows say.
- `plan-scope` voids a head on any run that did not pass, skipped and cancelled
  included (`trimtab/mergeauthority.py`, ADR 0043, ADR 0044).

## Decision

- `ci.yml` also runs on `ready_for_review` and `converted_to_draft`.
  `lint-test` has `if: !github.event.pull_request.draft`. A concurrency group
  per PR, with `cancel-in-progress: true`, cancels the run in progress. A push
  to `main` has no PR number, so each is its own group and none is cancelled.
- `claude-code-review.yml` also runs on `converted_to_draft`. Its job already
  skipped drafts and its group already cancelled.
- `plan-scope.yml` and `merge-authority-rerun.yml` do not change. `plan-scope`
  and `merge-authority` still run on a draft.

## Options considered

| Option | Why it lost |
|---|---|
| Keep running CI on drafts | The operator asked to stop |
| Leave `opened` and `synchronize` off `ci.yml`, keep only `ready_for_review` | No draft run at all, but a push to a ready PR would test nothing and its head would wait on `lint-test` forever |
| Run `lint-test` on a draft and fail it, so a draft's check is never a pass | Takes a runner per push, which is what the change removes, and marks every draft red |
| Gate `plan-scope` on draft too | A skipped run voids the head, so a plan PR whose last push came while it was a draft could never pass on the approver route without another commit |
| Gate only `merge-authority` on draft | The check that decides whose approval a PR needs would read as passing until the ready run registers. It cannot be cancelled on return to draft either, as a group would also cancel `plan-scope` |
| Cancel with a job calling the Actions API instead of a concurrency group | More code and an `actions: write` token, for what a group does with neither |

## Consequences

- A draft still shows a run for `ci.yml`, with its job skipped and no runner
  used. GitHub offers no way to start nothing.
- A draft still runs `merge-authority` on each push, and `plan-scope` on a
  `plan/` head.
- **The gate has a gap.** From marking ready until the new run registers,
  `lint-test` reads `skipped`, which passes. If GitHub never delivers the
  event, it keeps reading that way on a ready PR. `claude-review` has always
  had the same gap. Code-owner approval still applies, and a merger must read
  the check, not only the merge box.
- A broken head surfaces only at ready, the moment the billed `claude-review`
  starts. Local checks before ready are the only earlier signal.
- Going ready, to draft and ready again re-tests the same commit. Converting
  to draft and back is how a review is re-triggered, so that path now also
  costs a `lint-test` run.
- A push to a ready PR cancels the run on the previous head. Only that head's
  result is lost, and it can no longer merge.
- Other repositories keep their own workflows; each needs its own change and,
  where it runs `plan-scope`, the same reasoning.
- `tests/trimtab/test_draft_ci_workflow.py` pins the triggers, the groups and
  the conditions. `test_plan_scope_workflow.py` still pins that `plan-scope.yml`
  has no group.
