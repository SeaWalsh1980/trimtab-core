# 0042. The plan-scope check judges a plan run's PR diff against the base commit

## Status

Accepted on merge. Builds on ADR 0041
(`docs/adr/0041-session-modes-and-plan-scoped-writes.md`), which names a
server-side plan-scope check on the PR's diff as the gate for shell writes
and forged local git state; ADR 0041 is not edited. This change adds the
checker only. The workflow job that runs it changes a security control and
lands in its own PR in each repository (SEC-7).

## Context

guard-paths' plan-run layer judges each write inside the session. There the
agent can write through the shell, whose writing commands cannot be listed,
and can rewrite its branch name, `origin/HEAD` and refs. Neither matters if
the check moves to what reaches the platform: a PR's base commit and head
branch name come from the platform's event, which the agent cannot forge.

## Decision

`trimtab check-plan-scope --base <sha> --head <sha> --head-ref <branch>
--base-ref <branch> --default-branch <branch>` (`trimtab/planscope.py`)
judges `git diff --raw --no-renames base...head`. A
PR from a `plan/` branch fails when any changed path:

- is a layer 1 path (the harness and controls, as guard-paths' `hl_refused`
  lists them), whatever the lists say;
- is outside the base commit's write allowlist;
- is outside the plan's scope: its `.writes` declaration, plus this PR's own
  progress record (`<item>.md`) and register entries (`<item>-MM.md`, MM of
  two or three digits), where `<item>` is the branch's `item-NN-partK`;
  these are always create-only;
- is create-only on either list and is not a new file: a modification,
  deletion or type change of a file in the merge-base (a rename is a deletion
  and an addition), or an addition the base commit already holds;
- is a link or a submodule. The diff runs with `--ignore-submodules=none`,
  so no `submodule.<name>.ignore` setting can hide a gitlink change.

The allowlist, the plan and its declaration are all read from the base
commit. A missing one fails. A plan PR whose base branch is not the default
branch fails: a check run belongs to a commit, not to a PR, so a pass against
a branch whose lists the agent pushed would count for the same head's PR
into the default branch. Every input but `--head` comes from the event;
`--head` is required, because the job runs from a base checkout, where `HEAD`
is the base and the diff would be empty. A `plan/` branch that is not
`plan/<plan>/item-NN-partK` fails, as guard-paths refuses every write a run
makes on one; any other branch is skipped. A diff of more than 5,000 files,
or one git cannot read, fails closed (exit 2).

Globs are read as guard-paths' `hl_match_lines` reads them, with Python's
`fnmatchcase` standing in for bash's `[[ == ]]`: `*` spans directories in
both. Bash's `[^...]` is translated to `[!...]`. Backslash escapes and
bash's extended forms (`!(x)`, `@(x)`, which `[[ == ]]` enables) are read
literally, so they can only narrow a match; no allowlist uses them.

The job that runs it must run the checker from the PR's base commit, never
from the PR's tree, so a PR cannot change what judges it. It must also run
only for `plan/` heads (a job-level condition), so a non-plan PR's
`plan-scope` run concludes `skipped`, never `success`. The wrap-merged skill
accepts an approver merge only when both hold, and only for a PR whose head
is the record's `plan/<plan>/item-NN-partK` and whose base is the default
branch.

The bookkeeping scope is narrower than the hook's, which allows the plan's
whole progress and register folders. Narrower is safe: a run is never wider
than the gate, and one PR cannot write another item's record and steer which
record the next session reads as the latest.

## Options considered

| Option | Why it lost |
|---|---|
| Lists read from the PR's head | The head is what the PR controls: it could widen its own scope |
| Lists read from the merge-base, as the hook does | The base commit is what the PR merges into and holds the lists the admin approved last. If the admin narrows the allowlist or withdraws the plan after the branch started, the merge-base still holds the wider lists. The hook cannot see the event's base; the check can |
| Rename detection (`-M`) | A rename becomes one entry, and a check written against it can miss the deletion of the old path. With renames off the deletion is always seen |
| Reuse the bash matcher by calling guard-paths | The hook reads a tool call's JSON and the live tree, not a diff; extracting its matcher into a shared script widens a security control for no gain over a small, tested port |

## Consequences

- The matcher now exists twice, in bash and in Python. A parity test runs
  guard-paths' own `hl_match_lines` and `hl_refused` against the port on the
  same inputs, so a change to one that is not made to the other fails.
- A plan PR that is out of scope fails at merge, after the session has spent
  its work; the hook stays the early, in-session layer.
- Nothing is enforced until each repository's workflow runs the job and the
  operator makes `plan-scope` a required check.
