# 0049. Session end removes an autonomous run's scratch

## Status

Accepted on merge. Builds on ADR 0041
(`docs/adr/0041-session-modes-and-plan-scoped-writes.md`), which is not
edited. A security control (SEC-7): a new hook, `hooks/session-scratch.sh`,
registered in `settings.base.json`. Nothing is live until the instance's
pin moves past this change and `bootstrap.sh` runs.

## Context

An autonomous plan run keeps its temporary files only in its session
scratch directory: the scratchpad its harness gives it or, where the
harness gives none, one directory under the system temp directory named
after its session (Trimtab's rule for running an authorised plan). The
wrap-merged skill removes them after a merge, but only when the session
that did the work wraps it up. A run that stops, crashes or is cleared
leaves them behind. Cloud containers are discarded; a local machine keeps
everything.

A skill cannot fire on an event; a hook can. Claude Code's `SessionEnd`
payload carries the session id, the working directory and the transcript
path, but no scratch directory, and the desktop app's scratchpad layout
(`<tmp>/claude-<uid>/<project>/<session id>/scratchpad`) is not documented.

## Decision

`hooks/session-scratch.sh`, for autonomous runs only (the mode from
`hooks/session-mode.sh`, so a session that was a run at any point is one at
its end):

- **SessionStart (`start`)** creates `$TMPDIR/trimtab-run-<uid>/<session
  id>`, mode 700, and tells the session to keep temporary files in its
  harness's scratchpad or, where it has none, there.
- **SessionEnd (`end`)** removes that directory and the harness's
  scratchpad for this session, under `$TMPDIR` and `/tmp`. A directory is
  removed only when every directory on its path is real (no link) and owned
  by this user, the session id is a plain token, and the project directory's
  name starts with the harness's `-`. Nothing else goes: not the session's
  other harness files, not caches, not another session's scratch.
- Interactive sessions are left alone.
- Both events always exit 0. A removal that fails is reported on stderr
  (Claude Code's debug log) and the directory stays.

`session-mode.sh` gains `sm_payload`, which both hooks use to read their
payload. The liveness check names the new hook.

## Options considered

| Option | Why it lost |
|---|---|
| Only a Trimtab directory, created and announced at start | The harness's prompt tells a session to use its scratchpad, so a run would still leave files there |
| Only the harness's scratchpad | Relies wholly on an undocumented layout, and gives a run with no harness scratchpad nowhere sanctioned to write |
| Remove the whole `<session id>` directory, or the temp directory's stale entries | Wider than the run's own files; the plan rejected deleting anything wider, since that is riskier than leaving it |
| Leave cleanup to the wrap-merged skill alone | It runs only after a merge, in the session that did the work; a stopped or crashed run never reaches it |
| Remove scratch for every session | A person in an interactive session may want their scratch kept |

## Consequences

- A local run's temporary files go when its session ends, however it ends,
  provided Claude Code runs `SessionEnd`. A killed process runs no hook; the
  rule's fallback (name what is left in the progress record) still applies.
- A changed harness layout makes the scratchpad removal match nothing. It
  fails toward leaving files, never toward removing the wrong ones.
- A `/clear` ends the session and starts a new id, so the old session's
  scratch goes at the clear. A run keeps nothing across a clear by design.
- A run that leaves its plan branch keeps its scratch removal, through the
  session-mode record. A session whose record was pruned (idle past two
  days) and that has no other signal is taken for interactive at its end,
  and its scratch stays.
- `tests/guard-tests.sh` covers: creation and its mode, the announcement,
  interactive sessions untouched, removal off the plan branch (sticky),
  the rest of the session's harness directory and another session's
  scratchpad kept, a linked scratchpad, a linked project directory, a
  linked per-user directory, a project name without the dash, a session id
  that is not a plain token, and a garbage payload. A mutation trial broke
  five safety lines one at a time (the mode test, the scratchpad's
  ownership check, the leading-dash glob, the session-id test, and the
  real-directory test); a test failed for each.
