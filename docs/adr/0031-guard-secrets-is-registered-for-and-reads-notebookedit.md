# 0031. guard-secrets is registered for, and reads, NotebookEdit

## Status

Accepted on merge (2026-10-17). No trial is pending: the guard matrix
exercises the parse and the registration as the file ships. Closes the
guard-secrets gap that ADR 0030
(`docs/adr/0030-guard-paths-is-registered-for-and-reads-notebookedit.md`)
lists among its remaining gaps. The rest of 0030 stands.

## Context

ADR 0030 found that guard-paths never checked a NotebookEdit, for two
reasons: its matcher left the tool out, and its parse did not read the key
NotebookEdit names its target by. guard-secrets had the same two layers:

- **Registration.** `settings.base.json` registered guard-secrets on
  `Write|Edit|MultiEdit`, so Claude Code never ran it for a NotebookEdit.
- **Parse.** `extract()` read `file_path` for the path and `content`,
  `new_string` and each edit's `new_string` for the text. NotebookEdit sends
  `notebook_path` and `new_source`, so even when fed one, the guard saw an
  empty path and empty content. The path rules (`.env`, key files,
  `secrets/`) and the content rules (PEM blocks, tokens, signature packs,
  connection strings) all passed on nothing.

The matrix had no NotebookEdit case for guard-secrets.

Measured with the matrix of this change run against the previous guard and
`settings.base.json`: a NotebookEdit carrying a PEM block (also under
`tests/`, where tier 1 must not relax), a GitHub token, a production
connection string or a Zoho token in `new_source`, onto a `.env`, into
`secrets/`, and a cell delete in `secrets/` were all allowed (exit 0, 8
cases), with and without jq, and the registration check failed on
NotebookEdit alone.

## Decision

- The guard-secrets matcher becomes `Write|Edit|MultiEdit|NotebookEdit`.
- `extract()` reads the path as `file_path`, then `notebook_path`, and adds
  `new_source` to the scanned content, in both the jq and the python3
  branch. Only a non-empty string counts, in both, as ADR 0030 does for
  guard-paths: with jq's `//` alone, `file_path: ""` (or `0`, or `[]`) hid
  `notebook_path` from the jq branch, so a `.env` notebook was allowed with
  jq and blocked without it, and a test notebook lost its relaxation with jq
  only.
- A path that holds a newline or carriage return fails the parse, so the
  call blocks, as ADR 0030 decides for guard-paths and for the same reason
  it gives against collapsing the break. The path is line 1 of the parse and
  the content follows, so a Write of `/p/a\n/.env` was judged as `/p/a` and
  allowed in both branches. Found in review; older than this record.
- The matrix pins the registration as it ships: guard-secrets is registered
  once under PreToolUse with the 10 s timeout, its matcher matches Write,
  Edit, MultiEdit and NotebookEdit in full, and it matches neither Read nor
  Bash.

## Options considered

- **Fix the parse only, or the matcher only.** Rejected, for ADR 0030's
  reasons: either alone leaves a NotebookEdit unchecked in a session.
- **A catch-all matcher (`.*`).** Rejected. guard-secrets judges writes. On a
  Read its path rules would block an ordinary read of `.env.local` as "write
  to a .env file", and on Bash there is no content key to scan. The
  registration check pins Read and Bash out for that reason.
- **A separate hook for notebooks.** Rejected. It would be a second copy of
  the path and content rules that must stay in step with the first.
- **One payload parser shared by guard-paths, guard-secrets and
  guard-security.** Rejected here. It would remove the class of bug, but it
  changes three controls at once and each guard's fail-closed handling of a
  missing parser; that is its own decision.

## Consequences

- A NotebookEdit now meets the same path and content rules as a Write, with
  the same relaxation for test paths.
- A NotebookEdit that deletes a cell carries no `new_source`. Its path rules
  still apply, and with nothing to scan it is otherwise allowed.
- A writing tool added to Claude Code later still needs a matcher entry and,
  if it names its target or text under new keys, a parse change. The
  registration check pins today's tools only.
- A NotebookEdit with no `notebook_path` still parses to an empty path, as
  in ADR 0030; Claude Code requires the field. Not changed here.
- Each new block case was shown to be allowed by the previous guard: run
  against it, 9 assertions fail (8 NotebookEdit cases with exit 0 where 2 is
  expected, and the registration check on NotebookEdit), with and without jq,
  and every other case passes, the NotebookEdit allow cases included. The
  previous matrix still passes against this change, with and without jq.
- No new fork or exec: one more key in each branch's path and content.
- A session runs this matcher only after the installed `settings.json` is
  regenerated from a base that carries it.
