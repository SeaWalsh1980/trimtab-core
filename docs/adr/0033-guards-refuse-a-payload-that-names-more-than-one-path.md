# 0033. Guards refuse a payload that names more than one path

## Status

Accepted on merge (2026-10-17). No trial is pending: the guard matrix
exercises the decision in full. Amends ADR 0030
(`docs/adr/0030-guard-paths-is-registered-for-and-reads-notebookedit.md`)
for guard-paths. guard-secrets and guard-security take the same rule in their
own parse changes, each recorded with that change.

## Context

guard-paths, guard-secrets and guard-security each judge one target path,
taken from `file_path`, `notebook_path` or `path`, whichever comes first.
ADR 0030 made that the first non-empty string, which closed the cases where
an empty or non-string `file_path` hid a protected key. A non-empty decoy
still hid one. Reported by the automated review of the guard-secrets change:

- A NotebookEdit carrying `file_path: "/p/app/x.py"` beside
  `notebook_path: "<config>/hooks/x.ipynb"` was judged on the decoy and
  allowed by guard-paths, with and without jq. The same shape passed
  guard-secrets (a `.env` or `secrets/` notebook) and guard-security (a
  workflow notebook).

Claude Code's NotebookEdit schema allows no extra keys, so the shape is
probably not reachable through the tool. That is not verified, and the guard
cannot see which key the tool will act on.

## Decision

A payload that names more than one of `file_path`, `notebook_path` and
`path`, with any value but `null`, fails the parse, so the call blocks, in
both the jq and the python3 branch. Every built-in tool the guards are
registered for sends at most one of them.

## Options considered

- **Choose the key by tool name** (`notebook_path` for NotebookEdit,
  `file_path` otherwise). Rejected. An empty, unknown or future tool name
  must enforce (ADR 0002), and with no tool name to go on the guard would be
  back to guessing which key the tool reads.
- **Check every path present.** Rejected. The parse emits one path line;
  several would change the split in both branches, and guard-secrets' test-
  path relaxation would need a rule for paths that disagree. Refusing the
  ambiguous payload costs no real call.
- **Leave it, as unreachable through the schema.** Rejected. Each guard
  fails closed on its own input, and "the caller validates" is the
  assumption these guards exist not to make.

## Consequences

- A payload with `file_path: ""` and a `notebook_path` now blocks as
  ambiguous rather than being judged on the `notebook_path`. Both keys are
  named; no tool sends that.
- A `null` second key is not a second path, and is allowed.
- The matrix pins the count itself, not only the decoys: two harmless keys,
  and an empty `file_path` beside a harmless `notebook_path`, block. Checking
  every path or keying on the tool name would pass the decoy cases and allow
  these.
- Shown on the previous guard: the 3 decoy and 3 harmless-pair block cases
  fail with exit 0, with and without jq, and pass after; the null case
  passes on both.
- No new fork or exec: one count in each parse branch.
