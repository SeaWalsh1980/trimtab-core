# 0060. A plan run reviews in at most three rounds before each ready

## Status

Accepted on merge. Changes `wrap-merged`'s "Working a plan item" steps 4-5 and its paragraph on
sessions started for CI review. The CI review itself does not change.

Amended by [ADR 0070](0070-plan-skills-follow-the-run-owns-its-pr-flow.md): rounds now gate the
draft (one to five), with one round per CI fix and at most three CI fix rounds per item part.

## Context

`wrap-merged` step 4 said "at most three review rounds in session, then at most two CI fix
rounds", and step 5 stopped a run that "uses up a round limit". It did not say what a round
does with its findings, when the review ends early, whether the third round's findings are
fixed, or whether the limit counts per PR or per mark-ready. Each toggle to ready starts a
billed CI review (ADR 0052, ADR 0053), so the in-session loop is the cheap place to catch
findings, and an unbounded or ambiguous loop either loops or ships unreviewed fixes.

The doctrine's plan-run rule was changed to settle these questions, and the skill had drifted
from it.

## Decision

Before a run marks its PR ready, and again before each later return to ready, it reviews in one
to three rounds. A round runs `/trimtab-review` plus `/code-review` on the PR's current head.

- **Grade.** A finding's grade is the higher of the pass's own severity and the doctrine's
  finding-severity rule. The run may raise a pass's grade and never lowers it.
- **Out of scope.** Each finding outside the item's scope becomes its own register entry in
  that PR, unless an entry in that PR already records it. One graded CRITICAL or HIGH is also
  a stop, in any round.
- **Rounds 1 and 2** fix every finding within scope, whatever its severity, push, and start the
  next round. A round with no finding within scope ends the review.
- **Round 3** only reviews. A CRITICAL or HIGH among its findings is a stop; each MEDIUM or LOW
  becomes its own register entry.
- **Ready** once the last round left no CRITICAL or HIGH, in scope or out, every MEDIUM or LOW
  it left is a register entry, and the local checks pass. The rounds gate ready, not the draft.
- **One commit needs no further round:** the one that only adds the last round's register
  entries and the progress record's links to them, touching nothing outside the plan's register
  folder and that PR's progress record.
- **Limits.** At most three in-session rounds before each mark-ready and, separately, at most two
  CI fix rounds per PR. An infrastructure failure is re-run once and does not count.
- **Stops** now include: the third round ends with a CRITICAL or HIGH; a CRITICAL or HIGH outside
  scope in any round; the CI fix-round limit is exhausted.

## Options rejected

- **Fix round 3's findings in session.** The fix then reaches ready unreviewed, or needs a fourth
  round, and the limit stops being one.
- **Count the three rounds per PR rather than per mark-ready.** A PR that used its rounds before
  the first ready would have none left to review a CI fix, so a CI fix would reach ready
  unreviewed.
- **Let a pass's own severity stand.** A pass can under-grade a doctrine breach, and the run would
  then mark ready over it.
- **Log in-scope MEDIUM and LOW findings in rounds 1-2 instead of fixing them.** That moves cheap
  fixes into the register, where they wait for an operator.
- **Review the commit that only adds the register entries.** That review could find more, which
  would need more entries and another review: the loop never ends.
- **Keep the reasoning in the skill.** The skill is read by every plan run; the reasons are read
  only when the rule is questioned.

## Consequences

- More stops: any out-of-scope CRITICAL or HIGH now stops a run, even when the item is otherwise
  finished.
- The register-only commit reaches ready without a round. Nothing enforces its path bound: the
  guard's plan-run scope (ADR 0041) allows every path the plan declares, not only the register
  folder and the progress record, so a run that puts other changes in that commit gets them to
  ready unreviewed. Only the skill's text holds it, and CI review still sees the head. Accepted.
- The skill restates the rule rather than citing it, because the base cannot name an instance's
  IDs. It can drift again when the doctrine changes.

## Measurements

None recorded. The rule was the owner's call, not the result of a trial.
