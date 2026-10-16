# 0023. Routines run the platform's default model

## Status

Accepted (2026-10-14), by the operator. One consequence awaits a trial; see
"Trial, still open" below.

2026-10-16: the trial is recorded; see "Trial result" below. No clearing step
is needed.

## Context

Each routine template named a versioned model ID in
`job_config.ccr.session_context.model`. A versioned ID is correct on the day
it is written and stale from the next release: moving it takes a template
change, a base release, a pin move and an update of every routine rendered
from it. Until that lands, a routine either runs an older model than the
operator chose or differs from its render, and the drift shows up in every
comparison of a routine with its render.

A routine record with no model set runs the platform's default model. Routines
created before the templates named a model ran that way, unattended and
guarded, with no fault traced to the model.

ADR 0009 made the same choice for the CI review: the model is left unset so
that it follows what the platform ships, not a hand-kept ID.

## Decision

- **The routine templates name no model.** `session_context` carries no
  `model` key, so every routine rendered from them runs the platform's
  default model.
- **It holds for the conformance, retro and upstream-retro templates alike**,
  and a test pins the absence in each render.

## Options considered

- **A versioned model ID (the previous templates).** Rejected. Every model
  release needs a hand edit, a base release and a pin move before a routine
  follows it, and a routine updated by hand in between differs from its
  render.
- **The family alias `opus`.** Rejected by the operator. It removes the
  version, but it fixes the family by hand. A trial update showed that the
  routines API accepts the alias and stores it as typed, without resolving it:
  only a run shows which model the alias reaches.
- **The literal value `default`.** Rejected. It means the same as no key, and
  no routine has been updated or run with it, whereas no key is the state
  routines already ran in.

## Consequences

- No routine change is needed when a model is released.
- Accepted: the platform chooses the model. It may differ by account plan and
  may change with no change here, including to a smaller model. A run's log
  shows the model it ran, so a change is visible after the fact, not before.
- Accepted: a routine's cost per run follows the default model, not a choice
  made here.

## Trial, still open

Closed 2026-10-16: see "Trial result" below.

The routines API updates partially: an empty connector list in an update has
been seen to leave the connectors in place. An update whose `session_context`
has no `model` may likewise leave a model set by an earlier update. After
updating a routine from a render, read it back: if a model is still set, clear
it before enabling the routine, and record here which way it went.

## Trial result (2026-10-14 to 2026-10-16)

- A per-project routine held the alias `opus`, set by the trial update
  recorded under Options. It was then updated from a render on a base
  carrying this decision: the update's `job_config` had no `model` key
  anywhere. The read-back showed no model, in `session_context` or in the
  derived state. So an update that omits the model clears one set earlier;
  connectors, a separate top-level field, have been seen to behave
  differently.
- A second per-project routine, which never had a model, read back the same
  after its update.
- Every manual run of either routine since ran `claude-sonnet-5-5`, the
  platform's default at the time, as each run log's `init` line shows.

Result: the update clears the model. The read-back stays the check, as core's
`routines/README.md` asks, since connectors show that not every field is
replaced.
