# 0026. Bootstrap refuses an attribution override

## Status

Accepted (2026-10-14), by the operator.

## Context

The base sets `attribution` in `settings.base.json` so that what a run
writes carries no reference to the run (ADR 0022; ADR 0025 turns all
attribution off). The installer merges three layers, base, instance and
machine: dicts recurse and a later scalar wins. An instance or machine layer
that set `attribution` would therefore replace the base's `false`, or reshape
an object form, with no warning, and the installed settings would no longer
carry the base's choice. The bootstrap test read the value back only for a
fixture that sets no `attribution`.

## Decision

The merge refuses an instance or machine layer that sets `attribution` at
all, whatever the value, before anything is written or any link switches, as
it refuses `disableAllHooks`. The refusal names the layer. The base is the
only place the key is set.

## Options considered

- **Refuse the key in any later layer (chosen).** Holds whatever form the
  base uses, needs no knowledge of what each sub-key means, and is checked
  the same way in every layer.
- **A post-merge check that `attribution.sessionUrl` is still false.**
  Rejected. It only protects the object form: with the base at `false`, a
  layer's `{"commit": "…"}` turns some attribution back on while the check
  has no `sessionUrl` to read. It also lets a layer customise attribution the
  base turns off.
- **A post-merge check that the merged value equals the base's.** Rejected:
  the same effect as refusing the key, except that a layer repeating the
  base's value passes. That case is not worth a comparison step, and a layer
  that repeats it adds nothing.

## Consequences

- An instance or machine cannot customise commit or PR attribution; the
  base's setting holds on every machine. A project that wants attribution
  text writes it in its own prompts and commands.
- Migration: a `settings.instance.json` or `machine.json` that sets
  `attribution` fails bootstrap after the pin move, with `REFUSED: <layer>
  sets `attribution`, which the base owns`. Remove the key and re-run.
  `machine.example.json` says so.
- Accepted: a layer repeating the base's exact value is refused too.
