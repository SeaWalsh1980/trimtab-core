# 0065. A detector judges Bash writes against the write allowlist

## Status

Accepted on merge. Supersedes in part ADR 0039
(`docs/adr/0039-guard-paths-refuses-the-harness-and-enforces-a-write-allowlist.md`):
its rejected option "Confine Bash writes too, by spotting write verbs", as far
as it rejects any in-session check of shell writes, and its residual "Shell
writes outside an allowlist are not stopped in the session", which this
narrows. ADR 0039 is edited only to mark that and link here (ENG-7.3); ADR 0041
is not edited. It is a change to a security control (SEC-7): a new hook and
its registration in `settings.base.json`, authorised in chat for this change
on 2026-11-12.

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

`hooks/bash-writes.sh` (`trimtab/adapters/bash_writes.py`) runs on Bash.

**PreToolUse (`pre`)** records a snapshot for the repository of the call's
`cwd`, found as guard-paths' `hl_find_root` finds it (the nearest ancestor
holding `.git`). The snapshot holds:

- every path `git status --porcelain=v1 -z --no-renames --untracked-files=all`
  lists, and every tracked path `git ls-files -v` marks skip-worktree or
  assume-unchanged, each with a fingerprint of git's status and its lstat
  (type, size, inode, mtime and ctime in ns);
- every local branch and remote-tracking tip, and HEAD;
- the allowlist's text as guard-paths reads it, where a link at `.claude` or
  at the list, an unreadable or non-UTF-8 list, or one over 1 MiB, is recorded
  as untrustworthy;
- the session mode and branch from `hooks/session-mode.sh`'s `sm_classify`.

The snapshot lives in `<config>/guard-state/bash-writes/`, named by the sha256
of session ID and `tool_use_id`, private to the user, as pr-monitor's lists
are (ADR 0028). If no snapshot can be recorded, the call is blocked (exit 2):
its writes could not be judged. That covers a payload with no usable `cwd`,
git failing inside a repository, and the state directory failing.

**PostToolUse and PostToolUseFailure (`post`)** take the snapshot (removing
it) and judge two sets of paths:

- every path listed now that was not listed before or has a new fingerprint;
- every path changed by a commit the call made on any branch. That is a commit
  reachable from a tip that is new since the snapshot, and from no tip the
  snapshot held, nor from origin's default branch now. So switching to an
  existing branch, or pulling merged work, is not judged as the call's own
  writing. Merges are read by combined diff, so a merged branch's own changes
  are not judged either.

Each path is judged as `hl_check_abs` judges a write, using `trimtab.planscope`'s
parser and matcher, so the session and the server read globs one way:

- layer 1;
- an untrustworthy list;
- no list (refused only in a run);
- outside the list;
- outside a run's plan scope (the hook's scope: `.writes` plus the plan's
  progress and register folders);
- create-only and already on the merge-base.

The call is judged autonomous when either phase's classification says so, or
the judged repository is on a `plan/*` branch at either phase. The mode only
tightens (ADR 0041). PostToolUseFailure is registered because a command that
exits non-zero raises it instead of PostToolUse.

Every git call names the repository outright (`--git-dir`, `--work-tree`),
which outranks `core.bare`, `core.worktree` and steering variables. The
environment is hardened as `planscope._git` does it. Each phase has one 7 s
deadline for all its git calls together, inside the hook's 10 s timeout. A hook
killed at its timeout reports nothing, so the adapter gives up first and says
so.

**Results.**

- Any problem exits 2. Claude Code shows the stderr to the agent; the call has
  already run. The message names the paths and cites CFG-2 (undo it, or stop
  and ask the operator). In a run it also says this is a blocker (AGW-4).
- A doubt is reported as unjudged, also with exit 2. A doubt means:
  - no snapshot, or one that is malformed or written by another version;
  - git failing;
  - an unknown mode;
  - the deadline passing;
  - more than 20,000 paths, 5,000 tips or 200 new commits.
- `HOOK_ALLOW_HARNESS=1` lifts the detector, as it lifts guard-paths' harness
  layers.

`trimtab.planscope` now imports its git edge lazily, so its pure rules load
under `python3 -S` without PyYAML. `pr_monitor.state_dir` takes the state
directory's parts, so both adapters check their directories one way. At
SessionStart a missing detector is reported with the other optional hooks
("HOOKS MISSING"), not as "GUARDS OFF": it detects rather than prevents, and
the guards are whole without it.

## Options considered

| Option | Why it lost |
|---|---|
| Keep the residual and fix only AGW-4's text | Leaves CFG-2 and NRM-3 resting on instruction text; the incident that prompted this would recur unnoticed |
| Spot write verbs before the call (`>`, `tee`, `sed -i`, `cp`, `mv`) and judge their targets | ADR 0039's reason stands: never complete, since any program writes. It would also need a shell parser to find targets, and a miss is silent |
| An OS sandbox (bubblewrap, Landlock) confining the Bash tool to the allowlist | Prevents rather than detects, and would stand against a command built to evade. But it needs per-project tuning, and differs on macOS; Claude Code's own sandbox setting is a security control of its own. Worth revisiting; out of scope here |
| Revert a refused change automatically | A destructive write the agent did not choose; the doctrine prefers loud failure to silent repair, and the operator may want to see the change |
| Judge every dirty path after each call, with no snapshot | A path dirtied earlier by the operator, or by an allowed Write, would be reported on every later call; a rewrite of an already-dirty file would look the same as none |
| Judge in PostToolUse only, keyed by status bytes | Misses a second write to a file already dirty (status unchanged); the fingerprint is what catches it |
| Walk HEAD only for commits | Misses a commit moved onto another branch and HEAD reset, and judges the history of any existing branch the call switches to (found in review) |
| Let git find the repository (`rev-parse --show-toplevel`) | `core.bare` or `core.worktree` set by a command turned the detector off or pointed it at an empty directory, and a git failure read as "outside a repository" (found in review) |
| Use the server's per-item plan scope | The session's rule is the hook's wider scope (ADR 0042 explains why the server is narrower); the detector reproduces the hook, not the server |

## Consequences

**What it does.**

- A Bash write outside the allowlist, or outside a run's plan, is now reported
  to the agent in the session, whatever the verb: `sed -i`, `tee`, `>`, `cp`,
  `mv`, `python -c "open(...,'w')"`, a script, `git apply`, or a commit on
  any branch.
- It detects after the fact; it cannot prevent. A refused change is on disk
  until undone.
- It is a check on mistakes, not a defence against a command built to evade
  it. The merge gates stay the guarantee: CODEOWNERS and admin merge for
  interactive PRs, `plan-scope` for plan PRs.

**Routes a deliberate command can still take, each passing silently:**

- rewriting its own snapshot, which lives where only a shell path built at run
  time reaches it (as with the session-mode record, ADR 0041). The snapshot
  carries nothing the agent cannot forge;
- moving origin's default-branch ref in the same call (`git update-ref`), so
  its commits read as merged work;
- writing from a process the call left running, after the call returned. The
  next Bash call judges it only if it lands before that call's snapshot;
- writing into another repository: only the repository of the call's `cwd`
  before the call is judged (`git -C`, an absolute path, a `cd` inside the
  command).

**Residuals that are not evasion, accepted:**

- gitignored paths are invisible to `git status` (they cannot reach a PR
  either);
- a path changed before the call and restored to HEAD's content by it is not
  judged, since the outcome is HEAD's content;
- pulling other people's commits into a branch other than the default one
  (a fetch moves a remote-tracking tip, then a merge) judges those commits as
  the call's. This is loud, not silent, and a false refusal;
- a cancelled call raises neither post event (Claude Code's hooks reference),
  so its snapshot is pruned after 24 hours unjudged;
- parallel Bash calls in one repository may each see the other's writes; a
  path is then reported on both, never on neither.

**Dependencies.**

- The hooks reference names `tool_use_id` on PreToolUse, PostToolUse and
  PostToolUseFailure but does not say the pre and post values match. The
  design depends on it. If they ever differ, `post` finds no snapshot and
  reports every call unjudged: loud, not silent.
- guard-tests' matrix rows "a shell write is not judged against it" stay: they
  pin guard-paths, which still does not judge Bash. Their comment now points
  here. guard-paths' own header comment still says shell writes are left to
  CODEOWNERS; correcting it is an edit to a guard and is left to its own
  change.

## Measurements

On the workstation (Debian, Python 3.13, git 2.47; trimtab-core, 206 tracked
files, 2026-11-12):

- `git status --porcelain -z --untracked-files=all`: 20 ms cold, 3–5 ms warm;
  other single git calls 4–7 ms.
- `python3 -I -S -B -c ''`: 10–14 ms.
- `run_module.py … trimtab.planscope` with PyYAML on the path: 73–86 ms,
  which is why the judging imports are lazy and `planscope`'s git edge no
  longer pulls `capture.sources` in at import.
- First version, end to end, five runs each: `pre` 76–105 ms, `post` with
  nothing changed 95–148 ms. A separate `python3 -P` version probe in the
  wrapper cost 11 ms per phase and was dropped; the adapter checks the
  version itself.
- After review round 1 (both phases classify the session; `ls-files -v`, the
  branch tips and explicit `--git-dir` added): `pre` 132–201 ms, `post` 157–209
  ms. `pre` runs in parallel with the other PreToolUse hooks, so the wait it
  adds is less than its run time.
