# 0046. merge-authority re-runs only this PR's run, on outcome-changing reviews

## Status

Accepted on merge. Builds on ADR 0045
(`docs/adr/0045-reviews-re-run-the-pull-request-merge-authority-job.md`),
which is not edited. A security control (SEC-7). Trimtab's copy made the
same changes first (Trimtab ADR 0019); this brings trimtab-core's copy level
with it.

## Context

claude-review on trimtab-core#119 and a local review of Trimtab#167 found
four things in `merge-authority-rerun.yml`:

- The job-level `actions: write` replaces the workflow-level
  `contents: read`, so the token never had contents access, though the file
  read as if it did.
- After the last wait the loop exits without reading the status again, so a
  run that finished during that wait is reported as still going.
- Comment reviews and review edits re-run the job, though the checker
  ignores comment reviews and an edit never changes a review's state. In a
  private repository each costs about two billed minutes for nothing.
- The run lookup filters only by head commit and event. If two PRs share a
  head commit, the latest `pull_request` run may be the other PR's, and the
  approval on this one would not take.

## Decision

- `permissions: {}` at the workflow level; the job keeps `actions: write`.
- One more status read after the last wait.
- Triggers `submitted` and `dismissed` only, and the job skips a review in
  state `commented`.
- The lookup keeps only runs whose `pull_requests` include this PR, with the
  number passed to jq through the environment, never spliced into the
  filter.

## Options considered

| Option | Why it lost |
|---|---|
| Leave trimtab-core's copy as it is | The two copies of one control would differ for no reason, and core keeps the spurious failure, the wasted runs and the wrong-PR gap |
| Filter runs by head branch instead of PR number | Two PRs can come from the same branch into different bases; the number is exact |
| Splice the PR number into the jq filter after a digits check | Works, but passing it through `env` keeps every event value out of code, as the rest of the workflow does |

## Consequences

- A merged PR's runs list no PR, so a review after the merge finds no run
  and the job fails. That changes nothing: the PR is merged, and the check
  is not required.
- A fork's runs list no PR either, so its re-run fails here rather than on
  the read-only token; either way it needs the admin's bypass (ADR 0043).
- `tests/trimtab/test_plan_scope_workflow.py` pins the triggers, the
  `commented` skip, both permission blocks and the PR filter.
