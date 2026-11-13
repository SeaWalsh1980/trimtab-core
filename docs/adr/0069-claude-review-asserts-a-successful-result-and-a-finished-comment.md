# 0069. claude-review asserts a successful result and a finished comment

## Status

Proposed. This changes a SEC-7 control: the workflow that runs `claude-review`
(`.github/workflows/claude-code-review.yml`). The operator authorised the change in chat
("Start item 4" of the 2026-11-12 repo protection-gaps plan). `claude-review` does not
review the PR that carries it, so a human reviews the diff and the admin merges.

## Context

The job's closing step, "Fail closed when no review was posted", passed when any Bot
comment (issue or inline) was created after the run began. That is too weak:

- the action's tracking comment is created at the start and stays at "running" if the
  one-shot session ends early;
- another workflow replying as the same `claude[bot]` login during the run also counts;
- the action skips itself when the PR edits the workflow (its anti-tampering check) and
  still exits success.

Each leaves a required check green with no review posted. Security.md §2.5 requires
failing closed when uncertain. Trimtab already runs the stricter assertion.

## Decision

Port Trimtab's step unchanged. The job passes only when both hold:

1. the action's execution file exists and its last message is a `result` with subtype
   `success` and `is_error` false;
2. a Bot issue comment created since the run began links this run's URL, starts with
   `**Claude finished `, and no longer says "Claude Code is working".

The "Claude Code Review" step gains `id: claude` so the step can read its
`execution_file` and `conclusion` outputs. Nothing else in the workflow changes.

## Options considered

- **Reviews-API check, `commit_id == head.sha`.** Rejected: it fails clean reviews. The
  action posts issue and inline comments, not necessarily a PR review, so a clean run
  has no review object to match.
- **Run-URL match alone.** Rejected: the tracking comment carries the run link while it
  still reads "running", so a frozen comment passes.
- **Keep the current any-Bot-comment check.** Rejected: a required check that goes green
  with no review breaches Security.md §2.5 (fail closed).

## Consequences

- A frozen "running" comment, a skipped run, or a denied posting permission now fails
  the check.
- The check depends on the action's output shape and on the finished comment's text
  (`**Claude finished `). An action upgrade that changes either fails closed, not open;
  the pinned action version is unchanged.
- A PR that edits the workflow still goes unreviewed by CI and still fails the check.
  That is unchanged: a human reviews the diff and the admin merges with bypass.
- The assertion is not run-proven in core until the first review after merge.
