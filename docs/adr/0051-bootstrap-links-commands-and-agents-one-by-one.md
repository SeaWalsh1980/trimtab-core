# 0051. Bootstrap links commands and agents one by one

## Status

Accepted on merge. Extends ADR 0050 from `skills/` to `commands/` and
`agents/`, and records a limit of 0050's rules that applies to all three.
0050 is unchanged; this record reverses its "Per-skill links for
`commands/` and `agents/` as well" row. The installer is part of the
harness, so this change lands in its own PR.

## Context

ADR 0050 made `~/.claude/skills` a real directory with one link per base
skill, because a whole-directory link pushed the operator's own skills
aside. It left `commands/` and `agents/` as whole-directory links: the risk
was the same, but no operator had hit it.

The risk is the same. Claude Code reads user commands from
`~/.claude/commands` and user agents from `~/.claude/agents`, and nowhere
else. An install that finds a real directory there moves it to
`<dir>.bak.<timestamp>`, so the operator's own commands and agents stop
loading, silently. Each install path already exists for `skills/` and is
tested, so extending it costs a loop, not a design.

0050's rules also have an edge case that its record leaves out.
`made_by_install` treats a link into any trimtab-core tree as an install's,
because an earlier install from a checkout made it and the install record
names no base path. A link the operator made into a core tree looks the
same.

## Decision

- `~/.claude/commands` and `~/.claude/agents` are handled exactly as
  `~/.claude/skills` is under ADR 0050. Each stays a real directory with one
  link per base entry, named after it. Commands and agents are files
  (`trimtab-plan.md`); skills are directories. The installer links whatever
  top-level entry the base ships.
- A clash in any of the three refuses the install before anything is
  written, and names each clash (`agents/trimtab-planner.md is the
  operator's, …`). `--check` reports each state without changing it, as
  before.
- An earlier install's whole-directory link (into the store, the base, or a
  core checkout) is replaced by a real directory. A link the operator made
  for the whole directory is refused, not replaced.
- `hooks/` and `bin/` (from the base) and `rules/` (from the instance) stay
  whole-directory links: they are the harness's own, and nothing of the
  operator's belongs beside them. `output-styles/` keeps its
  whole-directory link too, but the base ships none today, so the loop
  skips it. Claude Code does read user output styles from
  `~/.claude/output-styles`, so a base that ships one should move it to
  per-entry links first, in its own change.

- A link counts as an install's when it points into the same directory of
  another core tree: a link in `agents/` into a core tree's `agents/`. A
  link in `agents/` into a core tree's `skills/` is the operator's.

**Known limit, for all three directories.** Suppose the operator links an
entry into the same directory of a trimtab-core tree (a fork or a second
checkout), and the link is named after its target. The installer takes it for an install's. Two
outcomes follow:

- the base ships an entry with that name: the link is relinked to this base;
- it doesn't: the link is removed as stale (`removed <dir>/<name>, which the
  base no longer ships`). The operator's target is never touched, only the
  link.

A link named otherwise (an alias) or pointing anywhere else is the
operator's, and stays. To keep a link into another core tree, give it a
different name.

## Options considered

| Option | Why it lost |
|---|---|
| Keep whole-directory links for `commands/` and `agents/` until an operator hits it (0050's choice) | The failure is silent: the operator's own commands just stop loading. The fix reuses tested code, so waiting buys nothing |
| One generic loop over every linked directory, `hooks/` and `bin/` included | Those hold the guards. A per-entry merge with operator files there would let a stray file beside the guards load, and nothing of the operator's belongs there |
| Tell an operator's link into a core tree apart from an install's by recording every link an install makes | Adds an install manifest, which must be kept in step with every directory and survive reinstalls from other trees, to cover a case no operator has. The limit is recorded instead, along with how to avoid it |

## Consequences

- The operator's own commands and agents load beside the base's. On a
  machine where an earlier install moved a real `commands/` or `agents/`
  aside, the operator can move their entries back from
  `<dir>.bak.<timestamp>` after re-running bootstrap.
- A base command or agent can never silently shadow one of the operator's,
  or the reverse: a clash stops the install until one is renamed.
- A command or agent removed from the base disappears at the next install,
  as a whole-directory link did.
- The known limit above stands: an operator's same-named link into another
  core tree is relinked or removed.
- `tests/bootstrap-env-tests.sh` adds seven cases:
  - commands and agents linked one by one beside the operator's own;
  - an install's whole-directory `commands/` link replaced;
  - an operator's agent file with a base agent's name refused, with nothing
    written;
  - an operator's directory with a base command's name refused the same
    way;
  - a stale command link removed while the operator's link stays;
  - links into the same directory of a core checkout relinked, while a link
    into another directory of it stays;
  - `--check` reporting a whole-directory `commands/` link and leaving it.

  0050's skills cases run unchanged through the same code.
