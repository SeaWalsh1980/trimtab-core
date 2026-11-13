# 0062. A review round runs /trimtab-review, /security-review and /code-review

## Status

Accepted on merge. Changes the round definition in `wrap-merged`'s "Working a plan item"
step 4. Amends ADR 0060's "A round runs `/trimtab-review` plus `/code-review`" without
editing 0060; the rest of 0060 stands. The CI review does not change here.

Amended by [ADR 0070](0070-plan-skills-follow-the-run-owns-its-pr-flow.md): rounds now gate the
draft (one to five), with one round per CI fix and at most three CI fix rounds per item part.

## Context

The instance's doctrine now requires every code review, interactive or in a plan run, to run
three passes: `/trimtab-review`, `/security-review` and `/code-review`. Before that, a round
ran only the first and last. Neither of those is a security reviewer: `/code-review` hunts
correctness bugs, and `/trimtab-review` checks the harness, tests and silent failure.

`wrap-merged` restates the round, so it drifts from the doctrine until it changes too.
`/security-review` is a Claude Code built-in, already in `trimtab lint`'s built-in command
list, so the reference resolves on every machine and in cloud sessions.

No measurement drove this. The operator asked for it.

## Decision

- A round runs `/trimtab-review`, `/security-review` and `/code-review` on the PR's current
  head, in parallel, with their findings folded into one report.
- A pass that fails or returns nothing usable is named in the report, and the round is
  incomplete until it runs. A round is never silently narrower.
- Grading, scope, the round limit and the stop rules are unchanged from ADR 0060.

## Options considered

| Option | Why it lost |
|---|---|
| Keep two passes in the skill | Drifts from the doctrine the skill restates |
| Leave security to CI review | CI runs after ready; a security finding then costs one of the two CI fix rounds |
| Run `/security-review` only on security-relevant diffs | Needs the run to classify its own diff; a miss silently skips the pass |
| Run the passes in sequence | Same findings, longer wall-clock; the passes are independent |
| Edit ADR 0060's decision text | A record is never rewritten; this one amends it |

## Consequences

- Each round costs one more pass in tokens and time, up to three times before each ready.
- Overlapping findings must be de-duplicated. A finding raised by two passes takes the
  higher grade.
- A security finding graded CRITICAL or HIGH in round three is now a stop it would not have
  been before.
- Instances pick this up only when they move their pin.
