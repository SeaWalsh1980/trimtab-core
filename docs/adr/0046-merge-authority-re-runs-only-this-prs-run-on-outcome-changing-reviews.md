# 0046. merge-authority re-runs only this PR's run, on outcome-changing reviews

## Status

Accepted on merge. Builds on ADR 0045
(`docs/adr/0045-reviews-re-run-the-pull-request-merge-authority-job.md`),
which is not edited. A security control (SEC-7). The same changes are
proposed for Trimtab's copy in Trimtab#167 (Trimtab ADR 0019); the two
copies match.

## Context

claude-review on trimtab-core#119 and local reviews of Trimtab#167 and of
this change found six things in `merge-authority-rerun.yml`:

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
- Two reviews seconds apart start two re-run workflows that can both find
  the run finished and both ask for a re-run. The second request fails
  while the first runs, so a review that came after the first re-run read
  the reviews (a dismissal, a request for changes) is never judged, and a
  pass can stand on a dismissed approval.
- A fork's runs list no PR, and the error told the reader that a new event
  would help, which it never does for a fork.

## Decision

- `permissions: {}` at the workflow level; the job keeps `actions: write`.
- One more status read after the last wait.
- Triggers `submitted` and `dismissed` only, and the job skips a review in
  state `commented`.
- The lookup keeps only runs whose `pull_requests` include this PR, with the
  number passed to jq through the environment, never spliced into the
  filter.
- A `concurrency` group per PR with `cancel-in-progress: false`, so re-runs
  for one PR queue. After asking for a re-run, the job waits (at most a
  minute) until the run shows as started, so the next queued re-run finds
  it going, waits for it and re-runs again.
- The error for a PR with no listed run names the fork case and the
  admin's bypass.

## Options considered

| Option | Why it lost |
|---|---|
| Leave trimtab-core's copy as it is | The two copies of one control would differ for no reason, and core keeps the spurious failure, the wasted runs and the wrong-PR gap |
| Filter runs by head branch instead of PR number | Two PRs can come from the same branch into different bases; the number is exact |
| Splice the PR number into the jq filter after a digits check | Works, but passing it through `env` keeps every event value out of code, as the rest of the workflow does |
| Retry the re-run request when it fails as already running | Needs to tell that failure from others by its message, and still races; queuing in a concurrency group orders the re-runs without parsing errors |
| `cancel-in-progress: true` | Would cancel a re-run workflow between its request and its wait, and gains nothing, since every re-run reads every review |

## Consequences

- A merged PR's runs list no PR, so a review after the merge finds no run
  and the job fails. That changes nothing: the PR is merged, and the check
  is not required.
- A fork's runs list no PR either, so its re-run fails here rather than on
  the read-only token; either way it needs the admin's bypass (ADR 0043).
- Changing `TRIMTAB_ADMIN` or `TRIMTAB_APPROVER` starts no run. A pass
  judged under the old logins stands until the PR's next event or review,
  so after a login change the operator re-runs `merge-authority` on every
  open PR. The workflow's header says so.
- `tests/trimtab/test_plan_scope_workflow.py` pins the triggers, the
  `commented` skip, both permission blocks and the concurrency group, and
  runs the PR filter itself through jq on a runs payload: this PR's latest
  run among several, a run that lists two PRs, and none for another number.
  Narrowing the filter to the first listed PR fails it.
