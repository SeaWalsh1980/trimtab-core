# 0040. Write-allowlist entries can be create-only

## Status

Accepted on merge. Amends ADR 0039
(`docs/adr/0039-guard-paths-refuses-the-harness-and-enforces-a-write-allowlist.md`)
by adding one entry form to the write allowlist; the rest of ADR 0039
stands, and ADR 0039 itself is not edited. This changes a security control
(`hooks/guard-paths.sh`); the operator authorised it in chat.

## Context

A repository's write allowlist (ADR 0039) grants a path or refuses it, with
nothing between. The operator wants some records to be writable only as new
files:

- **Decision records.** ENG-7.3 prohibits rewriting an accepted ADR; a
  superseded one is marked and linked forward. The operator's rule is that
  an ADR is rewritten only at a person's request, never on an agent's own
  initiative.
- **Progress records of an authorised plan.** The instance's agent-workflow
  rule prohibits editing an earlier progress record; each item adds its own.

A plain allowlist entry such as `docs/adr/**` lets an agent edit any of
those files, and the guard could not tell creating one from rewriting one.

## Decision

An allowlist line starting with `+` (spaces around it ignored) is
**create-only**. For a path it matches:

- a **Write that creates a new file** passes;
- a **change to a file new on this branch** passes: an Edit, MultiEdit,
  NotebookEdit or Write over a file that is absent from the tree at
  `git merge-base HEAD <origin's default branch>`. That covers a file
  written earlier in the session, in an earlier session, or in an earlier
  part of the same item, as long as it has not yet merged;
- a **change to a file already on the default branch** (present at that
  merge-base) is refused, with a message naming ADR 0040;
- a **Write over a link** is refused outright, since it writes the link's
  target;
- when the base cannot be found (no git, no `origin/HEAD`, no merge-base,
  as on a branch unrelated to the default), every change is refused: the
  guard fails closed to plain create-only.

The merge-base, not the current tip of `origin/<default>`, is the base
because it is what the branch started from: every merged file present in
the working tree came in through it, so a stale local copy of the default
branch cannot make an accepted file look new. git runs with the caller's
repository-steering variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`
and the object-directory ones) dropped, replace refs off
(`GIT_NO_REPLACE_OBJECTS=1`, since a replacement could show the base without
a file) and pathspecs literal. The three git calls run only for a change on
a create-only path, not on every tool call.

A plain entry that matches the same path wins, since it grants more. The
operator's `HOOK_ALLOW_HARNESS=1` export lifts create-only with the rest of
the harness layer, which is the elevated mode for a requested rewrite or
for marking an ADR superseded. CODEOWNERS already gives these paths to the
owner and the rulesets require code-owner review, so a rewrite that reaches
a pull request still needs the owner's approval.

## Options considered

| Option | Why it lost |
|---|---|
| Leave ADRs and progress records out of the allowlist | An agent could then not write a new ADR or its own progress record, which the plan-run rule requires |
| A separate append-only list file | A second file to protect and keep in step; a prefix on the existing list says the same thing in one place |
| Detect edits from content (only allow additions) | The guard sees a path, not the change; diffing content is semantic detection the guard's design rejects |
| Strictly create-only: no change to any existing file | An agent could not revise a record it wrote a minute earlier, in the same session or a later part of the same item, without the operator's export; the rule protects accepted records, not drafts |
| Record the files this session created (a state file keyed by the hook's `session_id`) | A clear, compaction or the next cloud session starts a new session id, so a multi-session item loses the record; the state file is one more thing an agent's shell could write to; and the record is made before the write is known to succeed |
| Ask GitHub which files the open PR adds (`gh pr view --json files`) | Needs the network and GitHub auth inside a hook, and a hook that times out does not block the call; it also does not work before the PR exists |
| Compare with the tip of `origin/<default>` instead of the merge-base | A stale local copy of the default branch could make a recently merged file look new; the merge-base already holds every merged file this branch has |
| Rely on CODEOWNERS alone | The owner reviews the merge, but the rewrite already happened in the session; the operator asked for it to be stopped there |

## Consequences

- Marking an ADR superseded, or correcting a typo in one, needs the
  operator's `HOOK_ALLOW_HARNESS=1`, by design.
- A record stays editable while its branch is unmerged, by any session
  working that branch, and becomes immutable once it reaches the default
  branch.
- The guard depends on `origin/HEAD` being set (as `git clone` does). A
  repository without it fails closed; `git remote set-head origin --auto`
  restores it. A shallow clone whose history stops short of the merge-base
  fails closed the same way until it is deepened.
- The base is local git state, which an agent's shell can rewrite: moving
  `refs/remotes/origin/HEAD` or `origin/<default>` to an older commit, or
  renaming an accepted file with `git mv` and committing the rename, makes
  an accepted record look new. These are shell routes, the same class as
  ADR 0039's residual. The pull request diff still shows the change to an
  existing file, so code-owner review at merge is the backstop.
- Shell writes are not confined by the allowlist (ADR 0039's residual): a
  `sed -i` on an existing ADR is not refused here. CODEOWNERS at merge is
  the backstop.
- The Trimtab instance's allowlist uses create-only entries for its decision
  records and plans; trimtab-core's for its decision records.
