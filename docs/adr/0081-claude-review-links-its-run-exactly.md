# 0081. claude-review links its run exactly

## Status

Proposed. This changes a SEC-7 control: the workflow that runs `claude-review`
(`.github/workflows/claude-code-review.yml`). The operator authorised the change in chat on
2026-11-14, naming it. `claude-review` runs under the PR's own copy of this workflow, so its
result on this PR is a self-review: a human reviews the diff and the admin merges.

## Context

ADR 0069's assertion selects the finished tracking comment with
`contains(env.RUN_URL)`, where `RUN_URL` ends in the run id. That is a prefix match: run
`123` also matches a comment linking run `1234`. A stale comment from another run, created
after this run began, could satisfy the fail-closed assertion with no review of this run
posted. The same defect is being fixed in Trimtab by a sibling change.

## Decision

Match the link exactly: split the body on the literal run URL and require at least one
occurrence to be followed by a non-digit or the end of the body
(`split(env.RUN_URL) | .[1:] | any(test("^([^0-9]|$)"))`). Nothing else in the step changes.

## Options considered

- **`test()` with the URL regex-escaped, then `($|[^0-9])`.** Rejected: the escape has to be
  written and kept correct in shell or jq for a URL full of `.` and `/`; splitting on the
  literal string needs no escaping, and the remaining regex has no variable part.
- **Match the exact link form the action writes.** Rejected: it ties the assertion to the
  action's markdown shape, so an action upgrade that rewraps the link fails closed for a
  reason unrelated to the run id. The digit boundary is the only property that matters.
- **Keep `contains`.** Rejected: the prefix match above.

## Consequences

- A comment linking a longer run id no longer counts. A link followed by `)`, `/`, `#`, `?`,
  `.`, whitespace or the end of the body still does. A link followed directly by a letter or
  digit does not; the action does not write one.
- `tests/trimtab/test_draft_ci_workflow.py` runs the workflow's own filter under `jq`
  against sample bodies, so a prefix match fails a test. The tests need `jq` on the machine.
- Trimtab's step stays on `contains` until its sibling change lands.
- The tests run the filter under system `jq`; CI runs it under `gh --jq` (gojq). The
  constructs used (`split`, `.[1:]`, `any`, `test`, `env.`) are common to both, but a future
  divergence would not be caught by these tests. Not run under gojq before this PR.
