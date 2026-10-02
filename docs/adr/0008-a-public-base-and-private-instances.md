# 0008. A public base and private instances

## Status

Accepted.

## Context

Trimtab started as one private repository that held two different things:

- **mechanism**: the `trimtab` Python package and its CLI, the guards and
  hooks, the `/trimtab-*` commands, the `trimtab-*` agents, `bootstrap.sh`,
  the routine templates and the tests;
- **one operator's deployment**: their doctrine (the rule files from which
  `HARNESS.md` is generated, and the instruction file), the list of
  consuming projects, routine and environment IDs, and repository names.

Trimtab is meant to be set up in, and cloned for, other projects and other
operators. Code belongs in version control. Per-deployment values do not
belong in reusable code, and a deployment's values were already turning up
in commits. Nothing could be published while code and doctrine shared one
private history.

The constraints:

- The repository is the live control plane. `~/.claude` links into it, the
  guards protect it (ADR 0002), and bootstrap installs only from a canonical
  checkout (ADR 0003).
- Cloud routines have no durable disk and start from fresh clones (ADR 0006).
- No new infrastructure unless it is needed.
- Every change to a security control needs the operator's explicit approval,
  in a pull request of its own.

## Decision

Split Trimtab into a **public base**, trimtab-core (this repository), and
**private instances**.

- **The base holds mechanism only**: the package, guards, hooks, commands,
  agents, bootstrap (the installer), routine templates, tests and this ADR
  series. It holds no doctrine and no deployment values. It is MIT-licensed,
  and its history starts fresh. Files were brought over by an explicit
  allow-list, never with history. The package keeps the name `trimtab`.
- **An instance holds doctrine and deployment values**: `rules/`,
  `CLAUDE.md`, the generated `HARNESS.md`, `instance.json`, the consumer list
  `consumers.json`, an optional private pattern file
  `guards/secrets.patterns`, the instance settings layer
  `settings.instance.json`, a per-host `machine.json`, its own ADRs, and a
  small `bootstrap.sh` shim (ADR 0003).
- **The package has no fallback for finding its instance.** It takes the
  instance root from `--instance`, then from `TRIMTAB_INSTANCE`, and
  otherwise stops with a typed error that names both. Bootstrap writes
  `TRIMTAB_INSTANCE` into the generated settings, because the value differs
  per machine. The package reads doctrine only from the instance, and code
  (agents, commands) only from its own checkout.
- **The instance pins the base.** `instance.json` records the base as
  `{repo, sha}`, with a full 40-hex SHA. Bootstrap installs that exact
  commit as a verified snapshot (ADR 0003). A project's lock pins the
  **instance** SHA. The instance's own pin fixes the mechanism, so one SHA
  reproduces both. Drift and bump compare doctrine paths only, so an instance
  commit that changes only deployment values does not make every project
  warn.
- **Settings are merged in layers**: the base's `settings.base.json` (hook
  registrations), then the instance layer, then `machine.json`. Lists
  concatenate, objects merge recursively and scalars override, so a layer
  cannot remove a guard's registration.
- **Guards are extended with data, not scripts.** Vendor token signatures
  with a distinctive, documented shape ship as packs in `hooks/secrets.d/`.
  All packs apply unless `TRIMTAB_SECRET_PACKS` narrows the list or turns
  them off with `none`. The instance's private patterns go in its pattern
  file. A malformed or missing source fails closed (ADR 0001).
- **Bootstrap checks the doctrine before switching anything.** It runs
  `trimtab registry --check` against the instance. On failure nothing is
  switched and the previous install stays live.

## Options considered

- **Keep one repository and move deployment values into variables.**
  Rejected. Code and doctrine would still share one private history, so
  nothing could be published without rewriting that history. Another operator
  would have to fork someone else's doctrine.
- **Give the base the existing repository's name and rename the private
  one.** Rejected. A rename means updating remotes on every machine and every
  routine, and moving a checkout had already switched the guards off once
  (ADR 0004). GitHub repository names are case-insensitive within an owner,
  so the two names also had to differ by more than case.
- **The lock pins the base SHA.** Rejected. The doctrine and its IDs would
  float, and bump and the overrides lint would lose their point of
  comparison.
- **The lock pins both SHAs.** Rejected. It adds a policy field and a second
  pin that can disagree with the instance's own.
- **The base as a submodule, a vendored copy, or a pip install.** Rejected,
  for the reasons in ADR 0003.
- **Instance-supplied guard scripts as the extension point.** Rejected. Each
  script would have to re-implement fail-closed handling, and neither the
  bootstrap self-probe nor the liveness check (ADR 0004) could see it.
- **Every vendor signature in the instance file.** Rejected. Every instance
  would rewrite and re-test the same public regexes.
- **Vendor signatures always on, with no switch.** Rejected. Instances that
  never use a vendor would get false positives.
- **Only the built-in signatures apply until an instance opts into packs.**
  Rejected. A machine that pulled the new guard before re-running bootstrap
  would silently stop blocking tokens the guard used to block. With the
  packs on by default, a machine without the switch loses nothing.
- **Fall back to the package's own checkout when it looks like an
  instance.** Rejected. A checkout with stray doctrine files would be read
  as an instance without anyone noticing.
- **Copy the existing ADRs into the base.** Rejected. They cite private
  doctrine, the numbering would have gaps, and a mistake in a public history
  is permanent. The base starts a fresh series.
- **On a registry failure, install the new mechanism but keep the old
  doctrine, or only warn.** Rejected. The first leaves a mixed state that
  drift and bump would have to explain. The second puts a broken doctrine
  live, and every PR check fails until someone notices.

## Consequences

- The reusable code holds no deployment values. An instance holds all of
  them, in `instance.json` and `consumers.json`.
- One instance SHA reproduces a project's doctrine and mechanism, in a
  local session and in a routine alike.
- Every machine has **two live trees**: the base snapshot and the instance
  checkout. The path guard protects both, plus every snapshot (ADR 0002).
- A base change reaches an operator only through an instance commit that
  moves the pin. That is deliberate: a base change is reviewed in the base
  and acknowledged in the instance.
- The guard code is public, so anyone can read what the guards block. The
  patterns were never secret, and an operator's private patterns stay in the
  instance.
- A routine needs network access to fetch the base at the pin.
- Accepted: someone running the base with no configuration gets every
  signature pack, and may see blocks for vendors they do not use until they
  narrow the list.
- Accepted: the lock field `trimtab_sha` now names the instance SHA, which
  reads oddly for an instance with a different name.
- A consuming project's CI cannot build the ID registry without read access
  to its private instance. This is not yet settled.
- A starter instance template and `trimtab init-instance` are planned, to be
  built once an instance has run on this layout. Until then, the base is
  published for reading and review rather than for installing.
