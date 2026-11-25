# 0082. `session-command` is a base skill

## Status

Proposed. Drafted by an agent from a review of the change that adds `skills/session-command/`.
It becomes Accepted when the operator confirms or amends the reasons marked "to confirm" and the
change merges; the reasons are the operator's, not the agent's.

The review's CRITICAL, HIGH and MEDIUM findings on the first copy were fixed at the source and
re-copied. The skill was then simplified at the source (see Consequences) and the simplified copy
was reviewed again: no CRITICAL or HIGH finding. Its MEDIUM and LOW findings on the skill's text
(a `~/` path quoted so that it does not expand, example details that may come from a real project,
loose graders, and smaller wording items) are fixed at the source and re-copied, or recorded here
as accepted, before this merges.

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
set for the plan skills: `SKILL.md` is about 150 lines, there is no `references/` directory, and
it ships its evals (6 cases) inside the skill directory. `tests/trimtab/test_skills.py` scopes those
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
| Ship it with a shape like the plan skills (short entry, `references/`) | Not rejected on merit; the copy is kept in the shape of its source so it stays checkable against it. Reshaping is a later change. |

## Consequences

- Every consumer gets a skill whose purpose is to make lifting a guard quick. That is an accepted
  risk, held down by the skill's no-flag list and by the operator reading the command before running
  it. The skill cannot tell the operator's own words from pasted text when both arrive in one
  message, so it treats a paste it cannot separate as all report and offers no flag from it.
- The base now carries the skill's text and its evals, so the base's checks apply to them: the public
  scrub and the doc-reference test. Both found real defects in the first copy (home paths in the
  evals, and the plans-directory literal), which were fixed at the source and re-copied.
- A review of this change found issues in the skill's text, fixed at the source and re-copied: a
  plan-run test that did not rule out every session-mode signal, deployment values in the examples,
  a non-credential claim taken from the pasted report, and accuracy and quoting items. The skill
  now cites the doctrine by item ID, because the base holds no rules.
- After those fixes the skill was simplified at the source: `SKILL.md` went from 334 to about 148
  lines, to five rules and an output shape, and the evals from 47 (235 graders, built up over
  several review rounds, many of them one eval per sentence of the skill) to 6. The accepted
  cost is coverage, and for one rule more than coverage:
  - The line saying `--force-with-lease` is never refused was removed from the text, not just left
    untested. `guard-bash` allows that push, so a report claiming it was refused no longer fails
    to match the table's guard-bash row, and the row maps it to `HOOK_ALLOW_DESTRUCTIVE=1`. An
    exported `HOOK_ALLOW_DESTRUCTIVE` lifts the push rules as well (`hooks/guard-bash.sh`, the
    session escape hatch runs before them), where the inline prefix does not.
  - The `HOME` check and the heredoc-delimiter rule stay in the text with nothing testing them.
  - These rules also lost every eval: the guard-github, GitGuardian, `plans/authorised/` and
    multi-refusal aborts; the rule that a mismatched flag named in a report is ignored; the rule
    that the secrets flag needs the operator's own words; `--continue` with a flag asking for a
    session id; the plan-run environment, announcement and path signals; and the prompt-file
    chain.
- No eval has been run against a model, and there is no eval runner in the base, so the six
  remaining cases are examples of intended behaviour, not a passing suite.
- The table in `SKILL.md` quotes the guards' own refusal text. It was checked against the base's
  hooks (`guard-paths`, `guard-bash`); the refusal text of `bash-writes` is not quoted, so a
  refusal from that hook is not matched by string.
- Rolling it out to an instance that still installs its own `session-command` needs that copy
  removed first, or bootstrap refuses it.
