# 0082. `session-command` is a base skill

## Status

Proposed. Drafted by an agent from a review of the change that adds `skills/session-command/`
(the reasons marked "to confirm" are the operator's to confirm or amend before this merges).
Accepted on merge, and only with the skill's open review findings resolved (see Consequences).

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
set for the plan skills: it is about 205 lines, has no `references/` directory, and ships its
evals (26 cases) inside the skill directory. `tests/trimtab/test_skills.py` scopes those
conventions to the three plan skills, so nothing enforces them here.

## Decision

The base ships `session-command`, copied from the instance. It is the base's own skill from then on:
fixes land here, and the instance's copy is removed when the pin that carries this one is taken
(bootstrap section 1b refuses a clash with an operator's own skill of the same name).

The skill keeps the rule it exists to serve: overrides are the operator's decision. It lists the
cases where no flag may be offered (an autonomous plan run, a real credential, a dismissed secret
alert), and fails closed when it cannot tell.

## Options considered

| Option | Why it lost (to confirm) |
| :-- | :-- |
| Keep it in the instance only | Operators of other instances meet the same guard refusals and have no helper that stays within the operator-only rule; each would write its own, and the copies would drift. |
| Ship no skill | The relaunch command is easy to get wrong (quoting, `--resume` against `--continue`, an override wider than the refusal) and the mistakes widen a guard. |
| Ship it with a shape like the plan skills (short entry, `references/`) | Not rejected on merit; the evals and the file size are kept as they are so the copy stays checkable against its source. Reshaping is a later change. |

## Consequences

- Every consumer gets a skill whose purpose is to make lifting a guard quick. That is an accepted
  risk, held down by the skill's no-flag list and by the operator reading the command before running
  it. The skill cannot tell the operator's own words from pasted text when both arrive in one
  message; that stays a limit of its design.
- The base now carries the skill's text and its evals, so the base's checks apply to them: the public
  scrub and the doc-reference test. Both found real defects in the first copy (home paths in the
  evals, and the plans-directory literal), which were fixed at the source and re-copied.
- A review of this change found issues in the skill's text that this record does not settle, and
  that are to be fixed at the source before merge: a plan-run test that does not rule out every
  session-mode signal; deployment values in the examples; a non-credential claim taken from the
  pasted report; and smaller accuracy and quoting items. Until they are fixed the status stays proposed.
- The skill's cites to the doctrine are by file and section. They should be by item ID, because the
  base holds no rules.
- Rolling it out to an instance that still installs its own `session-command` needs that copy
  removed first, or bootstrap refuses it.
