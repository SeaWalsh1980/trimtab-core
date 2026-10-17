# 0032. guard-security takes a string path with no line break

## Status

Accepted on merge (2026-10-17). No trial is pending: the guard matrix
exercises the parse in full. Applies to guard-security the parse decision
ADR 0030
(`docs/adr/0030-guard-paths-is-registered-for-and-reads-notebookedit.md`)
takes for guard-paths. ADR 0011
(`docs/adr/0011-guard-security-blocks-changes-to-security-controls.md`)
stands.

## Context

guard-security's parse emits the file path, the command and the tool name one
per line, then the command's segments, and reads them back with `read`, one
line each. Only the command's and the tool name's line breaks were handled.
Two faults followed, both found while reviewing ADR 0030's change, and both
older than it:

- **A line break in the path.** It pushed the path's tail onto the later
  lines. A Write to `<repo>/.github/workflows/x\n\nRead\n.yml` was read with
  tool name `Read`, which skips the file check, and was allowed in both parse
  branches, while the tool would write a `.yml` file into the workflows
  directory.
- **jq and python3 disagreed on the path.** jq's `//` treats `""`, `0` and
  `[]` as present, python3's `or` skips them, so a payload with
  `file_path: ""` and a `notebook_path` naming a workflow was allowed with
  jq and blocked without it.

Measured with this change's matrix against the previous guard: 6 cases allowed
with jq and 3 without (the python3 branch already skipped false-y keys).

## Decision

- The file path is the first non-empty string of `file_path`,
  `notebook_path` and `path`, in both branches.
- A path holding a newline or carriage return fails the parse, so the call
  blocks, whatever the tool.

## Options considered

- **Collapse the path's line breaks, as the command's are.** Rejected for
  ADR 0030's reason: the guard would check a path that is not the one the
  tool writes, and an exact control name such as `.claude/settings.json`
  would be compared against the wrong string.
- **Emit the fields NUL-separated, or the tool name first.** Rejected for
  now. It removes the class but changes the split here and in each guard
  that copies the layout; a path with a line break has no legitimate use.
- **Rely on guard-paths, which now refuses such a path for every tool this
  guard sees.** Rejected. That holds only while guard-paths is registered and
  its matcher covers this guard's; each guard fails closed on its own input.

## Consequences

- A Read of a path with a line break is not affected: guard-security is not
  registered for Read.
- A Bash command is unaffected; its line breaks are still turned into
  segment separators, as before.
- No new fork or exec: one test on the path in each branch.
