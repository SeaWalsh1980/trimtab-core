# 0018. Sessions reach the CLI on PATH, put there by a SessionStart hook

## Status

Proposed. Becomes accepted once a guarded cloud routine runs its method end to
end with `trimtab` resolved by this hook (see "Trial, still open" below).

## Context

The commands, agents and routine prompts ran the installed CLI as
`TRIMTAB="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bin/trimtab"`, then
`"$TRIMTAB" <subcommand>`. guard-paths refuses any Bash call whose text names
the config directory's `bin/` (ADR 0002): any reference, read-only commands
included, because a verb set is open-ended and a path set is not. Its one
exception is a bare call of `env-names.sh`.

`trimtab` was not on PATH. So with the guards live, no session could run the
CLI by any spelling, and no routine could run its method. It was found when a
guarded cloud routine bootstrapped cleanly, guards live, and then stopped at
its prompt's guard check, which asked it to run the CLI by that same path.
Interactive sessions had worked around it by running a scratch clone of the
package with `PYTHONPATH`, which a routine cannot do. The guard check itself
could only pass with the guards off.

Constraints: the file that runs must stay one the guards protect from writes,
and the fix must work in a routine's cloud sandbox, where the setup script
runs `bootstrap.sh` before the session and nothing else on the machine is
ours to configure.

## Decision

- **A SessionStart hook, `hooks/session-path.sh`, puts the config directory's
  `bin/` first on PATH** through `CLAUDE_ENV_FILE`, the file Claude Code runs
  before each Bash call of the session. The line names the directory
  absolutely (`%q`-quoted) and leaves `$PATH` to expand at that later run. It
  is written once per file, keeps other hooks' lines, and when the hook cannot
  write it, it exits 0 with a `systemMessage` naming why.
- **Sessions call the CLI by its name, `trimtab`.** A bare word holds no `/`
  or `.`, so guard-paths' path scan never sees it, while the file that runs is
  still `bin/trimtab` under the config directory, a protected root. guard-paths
  is unchanged.
- **bootstrap probes the hook end to end**, as it probes the guards: the env
  file it writes must resolve `trimtab` to this install's `bin/`. An install
  dies otherwise, so a routine's setup script fails loudly; `--check` reports
  it as drift.
- **The routine prompts' guard check proves enforcement**: the guard files and
  settings are read with the Read tool, `trimtab` must be on PATH by name, and
  a harmless call naming the config directory's `hooks/` must be refused by
  guard-paths, or the run stops.

## Options considered

- **An exemption in guard-paths for a bare call of the installed CLI**, as for
  `env-names.sh`. Rejected. It widens the most sensitive guard to a CLI with
  many subcommands, some of which write (`--apply`). The exemption would have
  to refuse shell metacharacters to stay narrow, which breaks the methods'
  `DOCTRINE=$(…) && …` idioms anyway. It would also have to match every
  spelling of the path (`~`, `$HOME`, `${CLAUDE_CONFIG_DIR:-…}`) exactly. And
  it is a guard change, with the review cost that carries.
- **bootstrap installs a launcher on PATH outside the config directory**
  (for example `~/.local/bin/trimtab`). Rejected. The launcher is a new
  executable that later sessions run, outside the protected set, so it is a
  write route around guard-paths unless the guard is extended to cover it,
  which is a guard change again. bootstrap would write outside the config
  directory and the store for the first time, adding a `--check` surface. And
  whether that directory is on PATH in a routine's sandbox is not ours to
  decide.
- **`env.PATH` in the generated settings.json.** Rejected. Claude Code copies
  settings `env` values as written, with no shell expansion, and they replace
  the inherited value. So it would freeze the PATH of whatever shell ran
  bootstrap into every session, and `--check` would report drift whenever it
  ran from a shell with a different PATH.

## Consequences

- No guard changes. The CLI keeps the protection it had, and the guard matrix
  pins both halves: the methods' calls by name pass, and writes, redirects and
  edits into the CLI or the config directory stay blocked.
- A session without the hook's line (an older install, or a Claude Code that
  does not provide `CLAUDE_ENV_FILE`) cannot run `trimtab`. It is told so at
  session start, every method's first call fails with `command not found`,
  and the methods stop rather than fall back to a path.
- Accepted: a session can still put its own directory ahead of PATH within one
  command (`PATH=/x:$PATH trimtab`). That runs code the session wrote, which a
  session can already do; it does not change what any later session runs.
- Accepted: a session can edit `CLAUDE_ENV_FILE` for its own later calls. The
  file is per session, so this reaches no other session.

## Trial, still open

The Claude Code documentation states that a SessionStart hook can write
`CLAUDE_ENV_FILE` and that each Bash call runs it first. It does not say
whether cloud sessions provide the file, or whether subagents' Bash calls run
it. Both are verified by the first guarded cloud routine run on a base that
carries this hook. If either fails, this record is revisited before any of the
rejected options is reconsidered, and the result is recorded here.
