# 0050. Bootstrap links base skills one by one

## Status

Accepted on merge. Changes how `bootstrap.sh` installs `skills/`; the other
linked directories are unchanged. The installer is part of the harness, so
this change lands in its own PR.

## Context

`bootstrap.sh` links each base directory Claude Code reads (`hooks/`,
`bin/`, `commands/`, `agents/`, `skills/`, `output-styles/`) as one link
under `~/.claude`, moving a real directory there aside first. Until the
wrap-merged skill (#101) the base had no `skills/`, so the loop skipped it
and `~/.claude/skills` stayed the operator's.

The first install of a base with `skills/` moved the operator's own skills
aside (`skills.bak.<timestamp>`), and Claude Code stopped loading them.
Operators keep third-party and personal skills in `~/.claude/skills`, which
is where Claude Code looks for user skills, so the base cannot own the
whole directory.

## Decision

- `~/.claude/skills` stays a real directory. Each base skill is linked into
  it by name, `skills/<name>` to the base's `skills/<name>`.
- An entry with a base skill's name that no install made (a real directory,
  a file, or a link outside the store, this base and any other
  trimtab-core tree) is the operator's. The install refuses, names it, and
  stops before writing anything (section 1b, ahead of `machine.json`), so a
  clash never leaves a half-switched install. A link into another core tree
  counts as an install's: an earlier install from a core checkout made it,
  and the install record names no base path. The tree is recognised by its
  `settings.base.json`, `hooks/guard-paths.sh` and `bootstrap.sh`.
- An earlier install's whole-directory link (into the store or the base) is
  replaced by a real directory. A `skills/` link the operator made is
  refused, not replaced.
- A link an install made whose skill the base no longer ships is removed.
  The operator's entries are never touched.
- `--check` reports each state without changing it.

## Options considered

| Option | Why it lost |
|---|---|
| Keep the whole-directory link; the operator moves their skills elsewhere | Claude Code reads user skills from `~/.claude/skills` only, so "elsewhere" means per-project copies or a plugin for every personal skill |
| Overwrite an operator's entry that has a base skill's name | Destroys the operator's file on a name collision; refusing costs one rename |
| Link the operator's skills into the base's directory | Writes into the snapshot store, which retention deletes |
| Per-skill links for `commands/` and `agents/` as well | The same risk exists there, but no operator has hit it; a separate change when one does |

## Consequences

- The operator's skills load beside the base's again. On a machine already
  bumped to a pin with `skills/`, the operator moves their skills back from
  the `skills.bak.<timestamp>` directory after re-running bootstrap.
- A base skill can never shadow an operator's skill silently, and the
  reverse: a clash stops the install until one is renamed.
- A skill removed from the base disappears from `~/.claude/skills` at the
  next install, as a whole-directory link did.
- `tests/bootstrap-env-tests.sh` covers: per-skill links in a real
  directory, the operator's skill kept, an install's whole-directory link
  replaced, a clash refused with nothing written, an operator's whole
  `skills/` link refused, a stale install link removed while the operator's
  link stays, `--check` leaving a whole-directory link in place, a
  dangling whole-directory link into a deleted snapshot replaced, and links
  into another core checkout (one skill, or the whole directory) relinked
  to this base.
