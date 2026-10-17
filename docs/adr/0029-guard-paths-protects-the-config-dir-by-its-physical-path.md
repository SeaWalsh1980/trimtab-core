# 0029. guard-paths protects the config dir by its physical path

## Status

Accepted on merge (2026-10-17). No trial is pending: the guard matrix
exercises the decision in full. Amends ADR 0002
(`docs/adr/0002-anchor-the-guards-to-the-live-control-plane.md`): its
"Under the config directory" set, and its "Matching is lexical" consequence
for the config directory's own names. Generalises the physical guard-state
root of ADR 0028
(`docs/adr/0028-guard-mcp-passes-set-monitor-only-for-prs-the-session-opened.md`)
to every config name. The rest of both stands. The NotebookEdit gap listed
under Consequences (#81) is closed by
[ADR 0030](0030-guard-paths-is-registered-for-and-reads-notebookedit.md).

## Context

ADR 0002 protects the config directory's names (`hooks/`, `rules/`, `bin/`,
`commands/`, `agents/`, `skills/`, `output-styles/`, `plugins/`, and the
files `settings.json`, `settings.local.json` and `CLAUDE.md`) by their path
as spelt under `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`. Paths are compared as
text.

The config directory can itself be a link, or sit under one: a dotfiles
checkout linked to `~/.claude`, a `CLAUDE_CONFIG_DIR` that names a link, or a
home directory on a linked volume. Then the same file has a second spelling,
through the link's target, and that spelling matched none of the protected
entries. Measured by sending PreToolUse payloads to `guard-paths.sh` with a
linked config directory, a Write, an Edit and a Bash redirect were all
allowed against every config name spelt through the target, and so was a
Bash `mv` or `rm -rf` naming the target directory itself.

The linked names were open too. `<target>/hooks/x` reaches the hooks tree,
because the kernel follows the `hooks` link, but it matches neither the
lexical root `$CLAUDE_CONFIG_DIR/hooks` nor the link's realpath root
`<checkout>/hooks`.

ADR 0028 closed this for `guard-state/` alone, after review, by resolving the
config directory with `cd -P` and adding `<target>/guard-state`. The other
names predate it and kept the gap.

## Decision

guard-paths resolves the config directory physically once, with the `cd -P`
and `pwd -P` builtins that ADR 0028 introduced. When the physical path
differs from the path as spelt, every config name is protected under both:

- the directory prefixes `hooks/`, `rules/`, `bin/`, `commands/`, `agents/`,
  `skills/`, `output-styles/`, `plugins/` and `guard-state/`;
- the exact files `settings.json`, `settings.local.json` and `CLAUDE.md`;
- the directory itself, matched by equality only and never substring-scanned,
  as the spelt form already is. Paths beneath it, the agent's memory, plans,
  todos and logs, stay writable.

The names are kept in one list, and the spelt entries, the physical entries
and the realpath loop over the linked names are all derived from it, so a
name added later is protected in every spelling at once.

The config directory that exists and does not resolve keeps ADR 0028's final
behaviour: every call blocks except a Read.

## Options considered

- **Keep the status quo: guard-state physical, the rest lexical.** Rejected.
  `settings.json` and `plugins/` change what a session executes, and they
  were as reachable through the target as guard-state was.
- **Protect the physical directory as a prefix.** Rejected for ADR 0002's
  reason: the prefix covers the agent's own state, and blocking it breaks
  memory and plan mode.
- **Resolve every target path physically at check time.** Rejected. It costs
  a fork per checked path where the resolution here costs one per call, and
  a Bash command has an unbounded number of path-shaped fragments, each of
  which would need one. A path that does not exist yet is not the obstacle:
  the snapshot-store check already resolves the deepest existing ancestor and
  appends the rest, and the same walk would serve here. Resolving the few
  roots once and comparing text keeps the cost per call fixed.
- **Refuse a linked config directory outright.** Rejected. A linked
  `~/.claude` or a `CLAUDE_CONFIG_DIR` that names a link is a legitimate
  layout, and blocking every call there removes the feature to close a gap
  that a second spelling closes.

## Consequences

- A Bash command naming the physical config directory by equality now
  blocks, as one naming `~/.claude` already did. A path beneath it does not.
- When the config directory is itself a link into a larger tree (a dotfiles
  checkout), that tree's root is the protected node, so a Bash command naming
  the checkout's root by equality blocks there. This is the trade ADR 0002
  already accepted for `~/.claude`.
- No new fork or exec. The `cd -P` already ran since ADR 0028. A linked
  config directory adds 13 string comparisons per checked path, and an
  unlinked one adds none, so no timing was taken; ADR 0002's cost figure
  still stands.
- Each new block case was shown to be allowed by the previous guard: run
  against it, all 38 new block cases in `tests/guard-tests.sh` fail with
  exit 0 where 2 is expected, and every other case passes.
- Gaps that remain. Matching is still textual: this record adds one more
  spelling, it does not make the check physical.
  - Any other link that names the config directory or a directory above
    it, including one the session creates, is a third spelling that
    matches no entry (#80).
  - NotebookEdit sends its target as `notebook_path`, which guard-paths
    does not read, so no path check applies to it (#81).
  - The linked trees' roots are protected only physically, so a spelling
    of the checkout through a linked ancestor misses them (#82).
  - `<symlink>/../<name>` is still collapsed as text (ADR 0002).
  - A `plugins/` or `guard-state/` that is itself a link: its target is
    still not a root. ADR 0028's recorder refuses a linked guard-state;
    plugins has no such check.
  - Renaming or replacing a directory above the config directory is not a
    path the guard can see.
