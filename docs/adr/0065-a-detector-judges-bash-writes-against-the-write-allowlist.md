# 0065. A detector judges Bash writes against the write allowlist

## Status

Accepted on merge. Supersedes one row of ADR 0039's options table
(`docs/adr/0039-guard-paths-refuses-the-harness-and-enforces-a-write-allowlist.md`,
"Confine Bash writes too, by spotting write verbs") in part, and narrows the
residual it accepted ("Shell writes outside an allowlist are not stopped in the
session"). ADR 0039 and ADR 0041 are not edited. It is a change to a security
control (SEC-7): a new hook and its registration in `settings.base.json`,
authorised in chat for this change on 2026-11-12.

## Context

guard-paths applies layer 2 (the default-deny `.claude/write-allowlist`) and
layer 3 (an autonomous run's plan scope) only to Edit, Write, MultiEdit and
NotebookEdit. Its Bash arm calls `hl_check` with the write flag off, so a shell
command reaches layer 1 only (the harness and controls, named literally). On
2026-11-12 an interactive session ran `sed -i '1s|…|…|' hooks/*.sh` through
Bash while Write and Edit to `tests/trimtab/…` were refused with "write outside
the repository's write allowlist".

That was the documented design (ADR 0039 Decision and Consequences; ADR 0041's
residuals), with CODEOWNERS at merge and, for plan PRs, the plan-scope check
(ADR 0042) as the backstop. But two rules say more than that:

- AGW-4 says the allowlist is "enforced by guard hooks in the session and
  CODEOWNERS at merge". For Bash nothing in the session enforced it.
- CFG-2 says a guard denial is final for that route, and NRM-3 says what must
  hold is enforced by a hook or a test, not by instruction text. Nothing
  noticed a Bash write to a path a Write had just been refused.

ADR 0039 rejected confining Bash by its verbs because the set of writing
commands cannot be listed. The set of paths a command changed can be: git
lists them.

## Decision

`hooks/bash-writes.sh` (`trimtab/adapters/bash_writes.py`) runs on Bash:

- **PreToolUse (`pre`)** records, for the repository of the call's `cwd`, every
  path `git status --porcelain=v1 -z --no-renames --untracked-files=all` lists,
  each with an lstat fingerprint (type, size, inode, mtime and ctime in ns),
  HEAD, and the allowlist's text as guard-paths reads it (a link at `.claude`
  or at the list, or an unreadable list, is recorded as untrustworthy). The
  snapshot lives in `<config>/guard-state/bash-writes/`, named by the sha256 of
  session ID and `tool_use_id`, private to the user, as pr-monitor's lists
  are (ADR 0028). If no snapshot can be recorded, the call is blocked (exit 2):
  its writes could not be judged.
- **PostToolUse and PostToolUseFailure (`post`)** take the snapshot (removing
  it), re-read the status, and judge every path that is changed now and was
  clean before or has a new fingerprint, plus every path changed by a commit
  the call made (reachable from the new HEAD, not from the old one or
  origin's default branch; merges by combined diff, so a merged branch's own
  changes are not judged as the call's). Each path is judged as
  `hl_check_abs` judges a write, using `trimtab.planscope`'s parser and
  matcher, so the session and the server read globs one way: layer 1; an
  untrustworthy list; no list (refused only in a run); outside the list;
  outside a run's plan scope (the hook's scope: `.writes` plus the plan's
  progress and register folders); create-only and already on the merge-base.
  The mode and branch come from `hooks/session-mode.sh`'s `sm_classify`, as
  guard-paths' do (ADR 0041). PostToolUseFailure is registered because a
  command that exits non-zero raises it instead of PostToolUse.
- Any problem exits 2. Claude Code shows the stderr to the agent; the call has
  run. The message names the paths, cites CFG-2 (undo it, or stop and ask the
  operator), and in a run says it is a blocker (AGW-4). A doubt — no snapshot,
  a malformed one, git failing, more than 20,000 changed paths or 200 new
  commits — is reported as unjudged, also with exit 2.
- `HOOK_ALLOW_HARNESS=1` lifts it, with guard-paths' harness layers.

`trimtab.planscope` now imports its git edge lazily, so its pure rules load
under `python3 -S` without PyYAML. `pr_monitor.state_dir` takes the state
directory's parts, so both adapters check their directories one way.

## Options considered

| Option | Why it lost |
|---|---|
| Keep the residual and fix only AGW-4's text | Leaves CFG-2 and NRM-3 resting on instruction text; the incident that prompted this would recur unnoticed |
| Spot write verbs before the call (`>`, `tee`, `sed -i`, `cp`, `mv`) and judge their targets | ADR 0039's reason stands: never complete, since any program writes. It would also need a shell parser to find targets, and a miss is silent |
| An OS sandbox (bubblewrap, Landlock) confining the Bash tool to the allowlist | Prevents rather than detects, but needs per-project tuning, and differs on macOS; Claude Code's own sandbox setting is a security control of its own. Worth revisiting; out of scope here |
| Revert a refused change automatically | A destructive write the agent did not choose; the doctrine prefers loud failure to silent repair, and the operator may want to see the change |
| Judge every dirty path after each call, with no snapshot | A path dirtied earlier by the operator, or by an allowed Write, would be reported on every later call; a rewrite of an already-dirty file would look the same as none |
| Judge in PostToolUse only, keyed by status bytes | Misses a second write to a file already dirty (status unchanged); the fingerprint is what catches it |
| Use the server's per-item plan scope | The session's rule is the hook's wider scope (ADR 0042 explains why the server is narrower); the detector reproduces the hook, not the server |

## Consequences

- A Bash write outside the allowlist, or outside a run's plan, is now reported
  to the agent in the session, whatever the verb: `sed -i`, `tee`, `>`,
  `cp`, `mv`, `python -c "open(...,'w')"`, a script, `git apply`, a commit.
- It detects after the fact; it cannot prevent. A refused change is on disk
  until undone. The merge gates stay the guarantee: CODEOWNERS and admin merge
  for interactive PRs, `plan-scope` for plan PRs.
- Residuals, accepted:
  - only the repository of the call's `cwd` before the call is judged; a
    write into another repository (`git -C`, an absolute path, a `cd` inside
    the command) is not;
  - gitignored paths are invisible to `git status` (they cannot reach a PR
    either);
  - a path changed before the call and restored to HEAD's content by it is
    not judged, since the outcome is HEAD's content;
  - commits a call makes that end up reachable from origin's default branch
    (a push to it) are excluded with merged work;
  - a cancelled call raises neither post event (Claude Code's hooks
    reference), so its snapshot is pruned after 24 hours unjudged;
  - parallel Bash calls in one repository may each see the other's writes;
    a path is then reported on both, never on neither;
  - a write by a process the call left running in the background, after the
    call returned, is judged by the next Bash call, or not at all;
  - the snapshot directory is under `guard-state/`, which guard-paths
    refuses to tools; a shell path built at run time can still reach it, as
    with the session-mode record (ADR 0041).
- The hooks reference names `tool_use_id` on PreToolUse, PostToolUse and
  PostToolUseFailure but does not say the pre and post values match. The
  design depends on it; if they ever differ, `post` finds no snapshot and
  reports every call unjudged — loud, not silent.
- Cost per Bash call: two Python starts plus one or two `git status` reads in
  the repository; the judging imports load only when a path changed.
- guard-tests' matrix rows "a shell write is not judged against it" stay: they
  pin guard-paths, which still does not judge Bash. Their comment now points
  here.

## Measurements

On the workstation (Debian, Python 3.13, git 2.47; trimtab-core, 206 tracked
files, 2026-11-12):

- `git status --porcelain -z --untracked-files=all`: 20 ms cold, 3–5 ms warm.
- `python3 -I -S -B -c ''`: 10–14 ms.
- `run_module.py … trimtab.planscope` with PyYAML on the path: 73–86 ms,
  which is why the judging imports are lazy and `planscope`'s git edge no
  longer pulls `capture.sources` in at import.
- The hook end to end, this repository, five runs each: `pre` 76–105 ms,
  `post` with nothing changed 95–148 ms. The adapter's imports are about
  30 ms of that (`-X importtime`) and its git calls about 17 ms. A separate
  `python3 -P` version probe in the wrapper cost 11 ms per phase and was
  dropped; the adapter checks the version itself. `pre` runs in parallel
  with the other PreToolUse hooks, so the wait it adds is less than its
  run time.
