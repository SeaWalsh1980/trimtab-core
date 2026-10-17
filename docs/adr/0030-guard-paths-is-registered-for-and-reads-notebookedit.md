# 0030. guard-paths is registered for, and reads, NotebookEdit

## Status

Accepted on merge (2026-10-17). No trial is pending: the guard matrix
exercises the parse and the registration as the file ships. Corrects a claim
in ADR 0002
(`docs/adr/0002-anchor-the-guards-to-the-live-control-plane.md`) and one in
ADR 0028
(`docs/adr/0028-guard-mcp-passes-set-monitor-only-for-prs-the-session-opened.md`),
and closes a gap ADR 0029
(`docs/adr/0029-guard-paths-protects-the-config-dir-by-its-physical-path.md`)
lists (#81). The rest of all three stands.

## Context

guard-paths enforces on every tool except Read, so that "an empty, unknown or
future tool name is checked by default" (ADR 0002). ADR 0028 relies on it:
a session's "Write, Edit, MultiEdit, NotebookEdit or Bash command cannot
write" `guard-state/`. Neither held for NotebookEdit, for two reasons, either
of which was enough on its own:

- **Registration.** `settings.base.json` registered guard-paths on
  `Read|Edit|Write|MultiEdit|Bash`. Claude Code runs a hook only for the
  tools its matcher names, so guard-paths never ran for a NotebookEdit.
- **Parse.** NotebookEdit names its target `tool_input.notebook_path`.
  guard-paths read `file_path`, then `path`, in both its jq and its python3
  branch, so even when fed a NotebookEdit payload it saw an empty path, and
  the control-plane check and the deny lists all passed on the empty string.

The matrix hid both. It fed the guard a NotebookEdit payload directly, so the
matcher was never in play, and built that payload with `file_path`, the key
the guard did read. guard-security was already registered for NotebookEdit
and read `notebook_path`.

Measured with the matrix of this change run against the previous guard and
`settings.base.json`: a NotebookEdit into the live hooks directory, into
`guard-state/`, into each of the eight config directories spelt through a
linked config directory's target, onto a `.env`, and into `secrets/` were all
allowed (exit 0, 12 cases), in both parse branches, and the registration
check failed on NotebookEdit alone.

## Decision

- The guard-paths matcher becomes `Read|Edit|Write|MultiEdit|NotebookEdit|Bash`.
- `parse()` reads the target as `file_path`, then `notebook_path`, then
  `path`, in both branches: the order guard-security uses.
- The matrix pins the registration as it ships: guard-paths is registered
  once under PreToolUse with the 10 s timeout, and its matcher matches each
  of Read, Edit, Write, MultiEdit, NotebookEdit and Bash in full.
- Every NotebookEdit case in the matrix uses `notebook_path`.

## Options considered

- **Fix the parse only, as the issue proposed.** Rejected. With the matcher
  unchanged the hook never sees a NotebookEdit, so the fix would pass the
  matrix and change nothing in a session.
- **Fix the matcher only.** Rejected. The guard would then run on
  NotebookEdit and still see an empty path.
- **A catch-all matcher (`.*`).** Rejected. It would run the guard on every
  MCP, Skill, Grep and Glob call, where `tool_input.path` means a search root
  rather than a file to write, so Read's exemption would no longer be the
  only judgement about which tools read. That is a wider change of behaviour
  than this gap needs, and its own decision.
- **A separate hook for notebooks.** Rejected. It would be a second copy of
  the path policy that must stay in step with the first.

## Consequences

- A NotebookEdit now meets the same path checks as an Edit: the live control
  plane, by every spelling ADR 0029 protects, and the deny lists.
- A writing tool added to Claude Code later still needs a matcher entry and,
  if it names its target under a new key, a parse change. The registration
  check pins today's tools only; it cannot name tools that do not exist yet.
- A NotebookEdit whose payload carries no `notebook_path` still parses to an
  empty path and is allowed, as an Edit with no `file_path` is. Claude Code
  requires the field, so this is not reachable through the tool, but the
  guard does not fail closed on it. Not changed here.
- Each new block case was shown to be allowed by the previous guard: run
  against it, 13 assertions fail (12 NotebookEdit cases with exit 0 where 2
  is expected, and the registration check on NotebookEdit), with and without
  jq, and every other case passes, the NotebookEdit allow case included. The
  previous matrix still passes against this change, with and without jq.
- No new fork or exec: one more key in each parse branch.
- Gaps that remain:
  - guard-secrets is registered on `Write|Edit|MultiEdit` and reads only
    `file_path`, so a NotebookEdit's `new_source` is never scanned for
    secrets. Same two layers; not changed here.
  - Grep is not in the guard-paths matcher. Whether a Grep naming a
    deny-listed file exposes its content has not been tested.
  - A session runs this matcher only after the installed `settings.json` is
    regenerated from a base that carries it.
