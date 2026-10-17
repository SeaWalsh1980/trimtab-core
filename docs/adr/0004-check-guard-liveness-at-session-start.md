# 0004. Check guard liveness at session start

## Status

Accepted. Amended 2026-10-17: the check also names the four non-guard hooks
the base registers, and a missing one of those reads `HOOKS MISSING` rather
than `GUARDS OFF`; see
[Amendment (2026-10-17)](#amendment-2026-10-17). Amended again 2026-10-17:
bootstrap runs the installed check and refuses an install where it stays
silent or names an installed guard as missing; see
[Amendment (2026-10-17, bootstrap self-probe)](#amendment-2026-10-17-bootstrap-self-probe).

## Context

`bootstrap.sh` links `hooks/`, `rules/`, `bin/` and the instruction file from
a checkout into `~/.claude`. If the checkout is moved after the install, for
example after a repository rename, every link dangles. Claude Code treats a
hook that cannot run as non-blocking (ADR 0001), so all the guards go off at
once. The next session starts with no guards and no doctrine, and no warning.

This happened. A checkout was moved, and the guard links and both
weekly-report systemd units dangled. Someone noticed only because the
session's instructions had changed and asked why. Re-running bootstrap from
the new path fixed it.

ADR 0003 stops bootstrap from installing out of a disposable worktree. It does
nothing about a canonical checkout that is moved later. `bootstrap.sh
--check` helps only when someone runs it.

## Decision

Register a **SessionStart hook whose command is written inline** in
`settings.base.json`, with matcher `*` (startup, resume, clear and compact).

It checks that each of the four guard scripts in `$HOME/.claude/hooks/` is
executable, and that `rules`, `bin` and `CLAUDE.md` in `$HOME/.claude` exist
and resolve. If anything is missing, it prints one JSON object with two
fields:

- `systemMessage`, shown to the operator. It starts with `GUARDS OFF` and
  names what is missing.
- `hookSpecificOutput.additionalContext`, given to the model. It says the
  session is unguarded, tells the model not to run state-changing commands,
  and tells it to ask the operator to re-run bootstrap.

If everything resolves, it prints nothing. It always ends with an explicit
`exit 0`. SessionStart cannot block, and a hook that reported an error would
bury the warning under an error notice. It uses only POSIX `sh` and `test`,
with no `jq` and no `python3`, nothing that could itself be missing.

It checks `CLAUDE.md` only, not `AGENTS.md`. Claude Code reads
`~/.claude/CLAUDE.md` as the user-level instruction file. An `AGENTS.md` there
is not loaded as user instructions. Accepting it would report all clear while
the doctrine was not loading, which is exactly the false negative this check
exists to catch.

## Options considered

1. **A script under `hooks/`.** Rejected. It would live where the failure
   happens: if `hooks/` dangles, the check dangles with it. The generated
   `~/.claude/settings.json` is a real file that bootstrap writes, not a link
   into the checkout, so it is the one place a move leaves intact.
2. **A script somewhere else in the checkout.** Rejected for the same
   reason: the checkout is what moves.
3. **`bootstrap.sh --check` in the shell profile.** Kept as a suggestion, but
   not enough on its own. It needs setting up on every machine, it runs once
   per terminal rather than per session, and it cannot tell the model
   anything.
4. **Block instead of warn**, for example with a PreToolUse hook that refuses
   every tool call while the guards are missing. Rejected. As a script it
   would have to live under `hooks/`, which option 1 rules out. Inline, it
   would add latency to every tool call to catch a rare event. Guards load at
   session start, so a warning at session start reaches the operator and the
   model at the only moment the state can change.
5. **Rely on `/hooks`.** Rejected as the only check. `/hooks` lists the
   registrations, which were still present during the outage. It cannot show
   that the scripts they point to are gone.

## Consequences

- It warns but does not block. An operator who ignores `GUARDS OFF` still
  has an unguarded session. The model is told to stop state-changing work,
  but that is an instruction, not enforcement.
- An inline shell command in JSON is harder to read than a script. That is
  the cost of surviving the failure it reports. The command is kept short,
  and the guard test matrix runs the exact string from `settings.base.json`,
  not a copy of it.
- It checks only that the guards can run, not that they work. Whether they
  work is the job of bootstrap's self-probes and the test matrix.
- It takes effect on a machine only after bootstrap regenerates
  `settings.json`.
- It checks `$HOME/.claude`, the path the guard registrations call. A session
  run with a different `CLAUDE_CONFIG_DIR` is outside its view.
- Changing `settings.base.json` changes Claude Code settings and hooks, which
  is a security control, so this change was made in a pull request of its
  own.

Measured in six test cases against a throwaway install: a healthy install
stays silent; a guard that is not executable is named; a moved checkout
names the dangling guards and rules; a machine with no install at all gets a
warning; and a restored install is silent again. Every warning is checked to
be valid JSON with both fields.

## Amendment (2026-10-17)

**Context.** The check named the five guards only (four when this record was
written; `guard-mcp` came with ADR 0013). `pr-body-check`, `doctrine-drift`,
`session-path` and `rule-usage` are registered too, and a missing one fails
the same silent way: Claude Code treats a hook that cannot run (exit 127) as
a non-blocking error.

**Decision.** The check's list names all nine hook scripts, in one `for g in`
loop, so the guard matrix still reads the list from it. Guards and the other
hooks get different messages:

- A missing guard, or missing `rules`, `bin` or `CLAUDE.md`, reads
  `GUARDS OFF` and names every missing path.
- When every guard is present and only `pr-body-check`, `doctrine-drift`,
  `session-path` or `rule-usage` is missing, the check reads `HOOKS MISSING`,
  names those, and asks for bootstrap to be re-run without telling the
  session to stop work.

Those four are named in the check; any other name in its list, one added
later included, counts as a guard. The check stays one inline POSIX command
ending `exit 0`. A guard-matrix case checks that every hook script
`settings.base.json` registers is in the list, so a hook registered later
cannot be left out of it silently.

**Options considered.**
- *One `GUARDS OFF` message for all nine hooks.* Drafted first, in #67, and
  rejected before it shipped: it tells a guarded session that every tool call
  is unguarded and not to run state-changing commands. A missing usage log
  leaves every guard in place, and an operator who sees `GUARDS OFF` for one
  learns to ignore it on the day the guards really are gone.
- *Count a hook as a guard by its `guard-` prefix.* Rejected: it fails open.
  A blocking guard added later under another name would, when missing, tell
  an unguarded session that its guards are installed.
- *Keep the list to the guards and add a second check for the rest.*
  Rejected: two inline commands to keep in step, and the guard matrix reads
  its list from one `for g in` loop.
- *Derive the list from the registered commands at session start.* Rejected:
  it needs a JSON parser, and this check deliberately uses only POSIX `sh` and
  `test` (Decision above).

**Consequences.**
- Two messages to keep correct instead of one; the guard matrix tests both,
  for each of the four soft hooks.
- A new non-guard hook must be added to the soft list too, or when missing it
  reads `GUARDS OFF`: the check can overstate a fault, never understate it.
- When `GUARDS OFF` fires for another reason, it lists any missing non-guard
  hooks with the guards, and the model's text calls them all guard hooks.
- The guard matrix stubs every name the shipped command lists, so it follows
  the longer list unchanged.

**Measured.** In the guard matrix, against a throwaway install, on top of the
cases above: each of the four soft hooks missing reads `HOOKS MISSING`,
names the hook in both fields, and never says unguarded or to stop work; a
guard and a soft hook missing together read `GUARDS OFF` and name both, in
both fields; the install is silent again once restored; a hook added to the
list without the `guard-` prefix is silent when present and reads `GUARDS OFF`
when missing; and every hook script `settings.base.json` registers is in the
list. The guard and soft lists are read from the shipped command, not
repeated in the test. Each new assertion was checked to fail on its
regression: classifying by the `guard-` prefix, printing only the guards in
the model's text, narrowing the soft list, adding "unguarded" to the note,
and dropping a registered hook from the list.

**Status.** Accepted. Amends the Decision's "each of the four guard scripts"
and the `GUARDS OFF`-only description of the two fields.

## Amendment (2026-10-17, bootstrap self-probe)

**Context.** The guard matrix runs the check as it is written in
`settings.base.json`, but nothing ran it as installed. The live
`settings.json` is merged from the base, the instance and the machine, and a
layer that drops or rewrites the command, or a base whose check never fires,
would install silently. Nothing then warns a session that its guards are
missing, which is the failure this record exists to catch.

**Decision.** Bootstrap reads the registered check out of the live
`settings.json` and runs it twice, under a fake `HOME`:

- With an empty `HOME`, it must print `GUARDS OFF` naming
  `hooks/guard-paths.sh`. If it stays silent, the install is refused.
- With a `HOME` whose `.claude` is this install, it must name no
  `hooks/guard-*` path. If it names one, the install is refused: every
  session would warn. A non-guard hook named here is a note, not a refusal,
  since the session stays guarded and that hook's own section reports it.
- No check registered at all refuses the install.

Under `--check`, each refusal is reported as drift instead, so `--check`
exits 1 on it.

**Options considered.**
- *Rely on the guard matrix.* Rejected: it tests the string in the
  repository, not what a machine installed after the layers merged.
- *Refuse when the installed run names any hook.* Tried in #67 and reverted
  after review. A base without `rule-usage.sh` was refused with a false
  "every session would warn GUARDS OFF", where the observer's own section
  only notes it.
- *Refuse when the installed run prints `GUARDS OFF`.* Rejected: `GUARDS OFF`
  also fires for a missing `CLAUDE.md`, which an instance may legitimately
  lack, and it then lists missing non-guard hooks too. That was the second
  trial in #67, reverted when a base without `rule-usage.sh` plus an instance
  without `CLAUDE.md` was refused.

**Consequences.**
- Two short `sh` runs per install and per `--check`.
- A missing `rules`, `bin` or `CLAUDE.md` on the install is not judged here,
  so an instance without `CLAUDE.md` installs while its sessions read
  `GUARDS OFF`. Bootstrap's `CLAUDE.md` section, not this probe, owns that
  link.
- This probe only notes a missing non-guard hook. Whether the install goes on
  is up to that hook's own section: a missing `rule-usage.sh` is a note, but a
  missing `pr-body-check.sh` or `doctrine-drift.sh` refuses the install, while
  a broken one of those two only ends the run NOT GREEN. That asymmetry is
  known, and not settled here.
- The fake `HOME` links `.claude` to the install, while sessions read the
  real `$HOME/.claude`. With a `CLAUDE_CONFIG_DIR` elsewhere (a sandboxed
  install), the probe proves the command against this install, not what those
  sessions see; the Consequences above already put such a session outside the
  check's view.
- A probe whose own setup fails (no temp directory, no payload) stops the run
  by name rather than probing the wrong thing.

**Measured.** In `tests/bootstrap-env-tests.sh`, against throwaway homes: a
check that never fires refuses the install, and `--check` exits 1 on it; a
check that always fires refuses the install; a complete install prints both
✓ lines; a base without `rule-usage.sh` installs with the note, with and
without an instance `CLAUDE.md`; and a `TMPDIR` that cannot hold a temp
directory stops the run naming the probe. The last three each fail against
the code before their fix.

**Status.** Accepted. Adds to the Decision; supersedes nothing.
