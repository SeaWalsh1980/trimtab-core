# 0004. Check guard liveness at session start

## Status

Accepted.

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
