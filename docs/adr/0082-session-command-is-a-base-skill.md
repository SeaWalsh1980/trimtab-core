# 0082. `session-command` is a base skill

## Status

Proposed. Drafted by an agent from a review of the change that adds `skills/session-command/`.
It becomes Accepted when the operator confirms or amends the reasons marked "to confirm" and the
change merges; the reasons are the operator's, not the agent's.

The skill's text was reviewed in rounds and fixed at its source in the instance, then simplified
there, and the simplified text was reviewed again: no CRITICAL or HIGH finding. Findings the review
left open on the skill's text (a `~/` path quoted so that it does not expand, the removed
`--force-with-lease` line, and smaller wording items) are fixed at the source and re-copied, or
accepted here, and they do not block this decision.

## Context

`session-command` writes the shell command and prompt an operator pastes into a terminal to start or
resume a Claude Code session, including a relaunch with a `HOOK_ALLOW_*` override exported at launch
after a guard refusal. It only writes text: it never runs the command, never exports an override
and never edits `machine.json`.

Until now it lived in an instance. Moving it into the base means every consumer's bootstrap links it
into `~/.claude/skills` (ADR [0050](0050-bootstrap-links-base-skills-one-by-one.md): one link per
base skill), so it reaches operators who do not run that instance.

The README says a base skill may carry the procedure an instance's rule requires, and that the rule
stays in the instance. No instance rule requires this skill: the overrides it names belong to the
guards the base itself ships (guard-security, guard-paths, guard-bash, guard-secrets, guard-mcp).

It also departs from the shape ADR [0070](0070-plan-skills-follow-the-run-owns-its-pr-flow.md)
set for the plan skills: `SKILL.md` is about 150 lines and there is no `references/` directory.
`tests/trimtab/test_skills.py` scopes those conventions to the three plan skills, so nothing
enforces them here.

## Decision

The base ships `session-command` as a single `SKILL.md`, taken from the instance. It is the base's
own skill from then on: fixes land here, and the instance's copy is removed when the pin that
carries this one is taken (bootstrap section 1b refuses a clash with an operator's own skill of
the same name).

The skill's evals stay in the instance and are not copied. Six cases remain there; the base has no
eval runner and nothing in it would run them.

The skill keeps the rule it exists to serve: overrides are the operator's decision. It lists the
cases where no flag may be offered (an autonomous plan run, a real credential, a dismissed secret
alert), and fails closed when it cannot tell.

## Options considered

| Option | Why it lost (to confirm) |
| :-- | :-- |
| Keep it in the instance only | Operators of other instances meet the same guard refusals and have no helper that stays within the operator-only rule; each would write its own, and the copies would drift. |
| Ship no skill | The relaunch command is easy to get wrong (quoting, `--resume` against `--continue`, an override wider than the refusal) and the mistakes widen a guard. |
| Ship the evals with the skill | An earlier copy shipped 181 eval files. They need a runner the base does not have, they were the source of most of the review rounds and of two public-base failures (home paths, a plans-directory literal), and none of the base's checks use them. |
| Ship it with a shape like the plan skills (short entry, `references/`) | Not rejected on merit; the text is kept in the shape of its source so it stays checkable against it. Reshaping is a later change. |

## Consequences

- Every consumer gets a skill whose purpose is to make lifting a guard quick. That is an accepted
  risk, held down by the skill's no-flag list and by the operator reading the command before running
  it. The skill cannot tell the operator's own words from pasted text when both arrive in one
  message, so it treats a paste it cannot separate as all report and offers no flag from it.
- The base carries only the skill's text, so the base's checks apply to it: the public scrub and the
  doc-reference test. Both found real defects in the first copy, fixed at the source.
- The skill was simplified at the source: `SKILL.md` went from 334 to about 148 lines, to five rules
  and an output shape, and its evals from 47 to 6. The base ships none of them, so nothing in this
  repository tests any of the skill's rules, and no eval has been run against a model.
- The line saying `--force-with-lease` is never refused was removed in that simplification. `guard-bash`
  allows that push, so a report claiming it was refused no longer fails to match the table's
  guard-bash row, and the row maps it to `HOOK_ALLOW_DESTRUCTIVE=1`. An exported
  `HOOK_ALLOW_DESTRUCTIVE` lifts the push rules as well (`hooks/guard-bash.sh`, the session escape
  hatch runs before them), where the inline prefix does not. Restoring the line is a change at the
  source, then a re-copy.
- The table in `SKILL.md` quotes the guards' own refusal text. It was checked against the base's
  hooks (`guard-paths`, `guard-bash`); the refusal text of `bash-writes` is not quoted, so a
  refusal from that hook is not matched by string.
- Rolling it out to an instance that still installs its own `session-command` needs that copy
  removed first, or bootstrap refuses it.
