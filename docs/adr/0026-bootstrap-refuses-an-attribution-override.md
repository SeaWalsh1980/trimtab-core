# 0026. Bootstrap refuses an attribution override

## Status

Accepted (2026-10-14), by the operator.

## Context

The base sets `attribution` in `settings.base.json` so that what a run
writes carries no reference to the run (ADR 0022). The value is an object:
at least `"sessionUrl": false`, and the base may also turn the default commit
and PR attribution off. The installer merges three layers, base, instance
and machine: dicts recurse, lists concatenate, and any other value from a
later layer replaces the earlier one. An instance or machine layer that set
`attribution` would therefore reshape the base's object (`{"commit": "…"}`
puts attribution text back, `{"sessionUrl": true}` the session link) or
replace it outright (`false`, `null`), with no warning, and the installed
settings would no longer carry the base's choice. The bootstrap test read
the value back only for a fixture that sets no `attribution`.

## Decision

The merge refuses an instance or machine layer that sets `attribution` at
all, whatever the value, before anything is written or any link switches.
The refusal names the layer (`REFUSED: <layer> sets `attribution`, which the
base owns`). The check is on the key in each layer after the base, not on
the merged result. The base is the only place the key is set.

## Options considered

- **Refuse the key in any later layer (chosen).** Holds whatever value the
  base uses, needs no knowledge of what each sub-key means, and is checked
  the same way in every layer.
- **A post-merge check that `attribution.sessionUrl` is still false.**
  Rejected. It protects one sub-key: a layer's `{"commit": "…"}` or
  `{"pr": "…"}` puts attribution text back that the base turns off, and a
  layer's `false` replaces the whole object with a value that Claude Code
  older than 2.1.281 does not read, so it skips the whole settings file,
  hooks included.
- **A post-merge check that the merged value equals the base's.** Rejected:
  the same effect as refusing the key, except that a layer repeating the
  base's value passes. That case is not worth a comparison step, and a layer
  that repeats it adds nothing.

## Consequences

- An instance or machine cannot customise commit or PR attribution through
  the installer's layers. A project that wants attribution text writes it in
  its own prompts and commands.
- Migration: a `settings.instance.json` or `machine.json` that sets
  `attribution` fails bootstrap after the pin move, with the refusal above.
  Remove the key and re-run. `machine.example.json` says so.
- Accepted: a layer repeating the base's exact value is refused too.
- Accepted: the installer governs only the user settings it generates. A
  project's own `.claude/settings.json` or `.claude/settings.local.json`,
  which Claude Code reads on top of them in a local session, can still set
  `attribution`; a multi-repository cloud session does not read it (ADR
  0022). Keeping it out of a project's files is that project's review.
