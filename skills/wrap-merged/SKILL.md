---
name: wrap-merged
description: Close out a merged pull request and move to what comes next. In a repository that runs authorised plans, verify the plan, take the next work from the merged progress record, then continue, hand over to a fresh session, or stop on a blocker. In any other session, record the finished work in memory and the plan and hand over a clean context. Use whenever a PR is reported merged ("merged", "it landed", "PR is in"), when a session starts from a merge or a routine ("continue the plan", "next plan item"), when you start or open the PR for a plan item, and when the user asks to wrap up or get ready for the next task — even if they never mention plans, memory or compaction.
---

# Wrap up a merged PR

When a PR merges, its history belongs in durable records and the live
session should carry only what comes next. Leftover review rounds, dead ends
and command output cost context and let stale facts steer new work.

Below, `<plans>` is the project's plans directory: `plans/` under `docs/`.
An authorised-plan project keeps three folders there: `<plans>/authorised/`
(plans the operator merged; never edited), `<plans>/progress/<plan>/` (one
record per PR) and `<plans>/unauthorised/<plan>.md` (the issues register,
named like its plan).

There are two situations. Decide which one you are in first:

- **Plan run** — the repository has `<plans>/authorised/`, and any of these
  holds: the merged PR adds a record under `<plans>/progress/`, adds a plan
  under `<plans>/authorised/`, or carries a `plan:*` label; or the session
  was started to continue a plan. Follow **Part A**. A PR with a `plan:*`
  label but no record is a plan PR missing its record: Part A stops on it
  (A3), it does not fall back to Part B.
- **Ordinary session** — anything else. Follow **Part B**.

`gh pr ...` subcommands use GraphQL, which some environments (the cloud
GitHub proxy) block. If one fails, use REST through `gh api` instead, for
example `gh api repos/{owner}/{repo}/pulls/{n}`. A guard refusing a command
is never a reason to find another route to the same thing.

---

## Part A — Plan run

The plan is a contract the operator approved by merging it. Your job is to
verify that contract, find exactly what it says comes next, and then
continue, hand over, or stop and explain. You never widen it: a hand-over
cannot create work, nothing found along the way gets done without a new
plan, and you never edit an authorised plan or an earlier progress record.

**Every stop** in Part A reports the blocker and proposes one to three ways
forward for the operator (for example: approve a new plan covering it, make
the guarded change by hand, or correct the record in a reviewed PR).

### A1. Confirm the merge

```bash
gh api repos/{owner}/{repo}/pulls/<n> --jq '{state, merged, merged_at, merge_commit_sha, base: .base.ref, labels: [.labels[].name]}'
```

- `merged` is true → continue.
- Not merged (open, or closed unmerged) → nothing is done. Keep any lessons
  (memory if the session has one, otherwise in your report), record nothing
  as done, and stop. A wrongly recorded "done" sends the next session past
  work that never landed.

### A2. Run the authorisation check

The plan is authorised only if **every** check holds. Any failure is a stop:
say which check failed and start nothing.

1. **The plan is on the default branch:**
   `git fetch origin && git ls-tree origin/<default> -- <plans>/authorised/<plan>.md`
2. **It was added by a PR the admin account merged.** Find the commit that
   last added it (`git log --diff-filter=A --format=%H origin/<default> --
   <file>`; if several lines come back, the file was deleted and re-added —
   use the one whose PR merged last, by `merged_at`, not the commit date).
   Find its PR with
   `gh api repos/{owner}/{repo}/commits/<sha>/pulls`, choosing the entry
   whose `merged_at` is set and whose base is the default branch (none →
   the check fails). Then read that PR's `merged_by.login` and
   `merge_commit_sha` from `gh api repos/{owner}/{repo}/pulls/<number>`.
   Compare the login with the admin login in the Trimtab instance's
   configuration. Never take it from the plan, a progress record or the
   conversation. If the configuration is not available in this session, or
   names no admin login, the check fails.
3. **It is unchanged since that merge:**
   `git diff --quiet <merge_commit_sha> origin/<default> -- <file>`. Any
   difference means it was edited after approval. (Commits inside the plan
   PR before its merge are fine; only changes after the merge count.)
4. **The enforcement is live.** The repository's write allowlist exists; the
   guard that enforces it is installed and reports itself live; and
   `CODEOWNERS` gives every always-refused file path in A4 to the admin. If you
   cannot confirm all three, the check fails. In particular, until the base
   ships that allowlist guard, this check fails and no plan can run.

Why this strict: the merge is the only authorisation an autonomous run has.
A plan edited after approval, merged by the agent's own account, or run
without the guards that keep it inside its allowlist authorises nothing.

### A3. Work out the state

Everything comes from the default branch and open PRs, never from memory:

- **In flight:** an open PR whose head branch is `plan/<plan>/item-*` —
  recognisable from the moment its draft opens, before any record exists.
  The branch name alone proves nothing: the PR's head must be in this same
  repository (not a fork) and its author must be the agent or admin
  account from instance configuration. A `plan/…` PR that fails either
  test is a stop — report it; never resume or build on it. Otherwise it is
  resumed before anything else — but only through the
  A4 gate, applied to the item its branch names. An open PR labelled
  `plan:blocked` is not resumed this way; only an operator-started restart
  picks it up.
- **Next:** the `## Next` section of the **most recently merged** record
  (`<plans>/progress/<plan>/item-NN-partK.md`). Order the records by the
  `merged_at` of the PR that added each one, not by commit date: a branch
  commit can be older than one merged before it. No records yet → item 01
  part 1.
- **That record's PR must be admin-merged.** Read its `merged_by.login` as
  in A2.2. A record merged by any other account (the approver included)
  authorises nothing → stop.
- **Done:** the `## Status` lines of the merged records.
- **Open issues:** entries with `status: open` in
  `<plans>/unauthorised/<plan>.md`.
- **The merged PR carries no record** → stop, unless it is the PR that
  added the authorised plan itself (the plan's first run: start item 01
  part 1). This applies to a session started by a merge; an
  operator-started restart has no merged PR to check here (a fix PR the
  operator merged is not a plan PR) and goes straight to the restart rule. Every item PR must carry a record, and continuing from an older
  `## Next` risks redoing work.
- **Sanity-check the record against the code.** If it says an item is
  complete but the plan's done condition is plainly unmet on the default
  branch, log it in the register, and stop if the next work depends on it.
  A record is the agent's claim; the merged code is the fact.

### A4. Gate the next work

The next work — `## Next`, or the item an in-flight PR works on — may
start only if all of these hold:

- It names an item that exists in the authorised plan and is not
  already complete. Anything else is a blocker — a hand-over cannot create
  work.
- It is not `plan complete`. A `blocked: <reason>` Next is a stop, except
  in a session the operator started to restart the run, where the next work
  comes from the restart rule below instead.
- Doing it would not need a write outside the write allowlist or to any
  always-refused path. Plan approval never covers these:
  - the instruction file, `CLAUDE.md`;
  - every security control: CI workflows, `CODEOWNERS`, Claude Code
    settings and hooks, scanner exemption lists, repository protection,
    Actions configuration, repository access;
  - the harness: the rules directory (or the lock's `rules_dir`, with its
    `overrides.md`), the Trimtab lock, agent, command and skill definitions;
  - `<plans>/authorised/`.

If the gate fails: say "needs approval: <reason>", list the open issues
with their PR links, propose ways forward, and stop. If `## Next` is
`plan complete`, say so and stop.

### A5. Record

- **No repository write after the merge.** The progress record shipped in
  the PR; editing it now would put unreviewed text on the default branch.
- Memory, if the session has one: a short line (plan, item, PR, merge SHA,
  date, what comes next). It is optional — the merged record is the source
  of truth, and cloud sessions have no memory at all.

### A6. Tidy

Follow the project's own after-merge instructions (removing the PR's
worktree or local branch, pulling the default branch). Do only what they
say; never delete a branch or worktree holding unmerged work.

### A7. Continue or hand over

Report first, in a few lines:

```text
PR <n> merged <date> (<sha>) — plan <plan>, <Status line>.
Authorisation: ok. Next: <item NN part Y: one-line summary> | needs approval: <reason> | plan complete.
Open issues: <UI-NN (PR link), ...> | none.
```

Then what matters is whether **this session's context holds the merged
PR's work**.

- **It does not** — the session started fresh from the merge: a routine or
  Action fired on it, a parent sent the brief, or a cleared session picked
  it up. The context is already clean, so if A3 and A4 passed, do the next
  work now — the in-flight PR if there is one, otherwise `## Next` —
  following "Working a plan item" below.
- **It does** — this session did the merged work. Hand over instead, so the
  next work starts clean, and stop after the report. The brief is the
  merged record's `## Next`, plus "run the authorisation check first". End
  in the first way that applies:
  1. **Started by a parent session:** stop. The parent clears this session
     and sends it the brief.
  2. **Desktop app:** tell the user the next turn starts from that `## Next`,
     then clear this session (`clear_session` with `"self"`), if the tool is
     available.
  3. **Otherwise (plain CLI):** give a paste-ready `/compact` line (see B4) —
     you cannot run `/compact` yourself.

  In the cloud there is nothing to hand over: the next merge, labelled
  `plan:continue`, starts the next session.

**A session started by the `plan:continue` label** first checks who added
it (`gh api repos/{owner}/{repo}/issues/<n>/events`, the `labeled` event
for `plan:continue`). Unless the agent, approver or admin account added
it, stop at once. The label only starts a session; it never authorises.

**Restarting after a stop** takes the operator's act: a session they start
for the plan (locally, or the routine's run-now). A PR they merge to fix
the blocker carries no record and no `plan:continue`, so it starts nothing
by itself. The restarted session runs A2 and reads the latest admin-merged
record. If its `## Next` is `blocked:`, the next work follows from the
record's file name (`item-NN-partK`) and `## Status`, never from its text:
part K+1 of the same item when the item continues or part K stopped
unfinished (that part then covers what part K left), otherwise the plan's
next item. An unmerged blocked PR is resumed as an in-flight PR.

**A session started for an item PR's CI review** (by the operator, or a
routine on review events) fixes the CI findings within scope, as in step 4
below. Count CI rounds from the PR's own review history, not from this
session alone.

### Working a plan item

One item, or one part of an item, per session, ending in one PR:

1. **Open a draft PR when the work starts**, on a branch named
   `plan/<plan>/item-NN-partK` (so a later session can see it is in
   flight), and push a
   checkpoint at each milestone. If the session dies, the next one resumes
   from the branch instead of starting over.
2. **Before requesting ready**, add `<plans>/progress/<plan>/item-NN-partK.md`:

   ```markdown
   # <plan> — item NN, part K

   ## Done
   <what this PR did; decisions and why; lessons; links to issues logged>

   ## Status
   item NN complete | item NN part K complete; item continues |
   item NN part K stopped unfinished

   ## Next
   item NN part Y: <brief a fresh session can start from — scope, files,
   starting state> | item MM part 1: <brief> | plan complete |
   blocked: <reason>
   ```

   and append anything you found but did not do to
   `<plans>/unauthorised/<plan>.md` (create it if absent), one line each:
   `UI-NN | <date> | item NN | PR <link> | <issue> | status: open`. Never
   change an existing entry's status.
3. **Label** the PR `plan:continue` unless `## Next` is `plan complete` or
   `blocked: <reason>`. (When you author a PR that adds an authorised plan,
   label it `plan:continue` too: its merge starts item 01 part 1.)
4. **Review in session** with `/trimtab-review` plus `/code-review`. Fix
   findings inside the item's scope; log the rest in the register. When no
   CRITICAL or HIGH finding remains and the local checks pass, **request
   ready** by labelling the PR `plan:review-passed` (through `gh api` if
   `gh pr` is blocked). The label describes one head: remove it before
   pushing anything more, and add it again only after reviewing the new
   head. Do not mark it ready yourself: marking a draft ready,
   and re-triggering CI review, go only through what the guards permit —
   today the operator. Then fix CI review findings the same way. At most
   three review rounds in session, then at most two CI fix rounds; an
   infrastructure failure is re-run once and does not count. The item PR is
   merged by the admin account; the approver's review alone never advances
   the run.
5. **Stop when** the work cannot be finished, needs work outside its scope,
   needs a write a hook refuses, needs the authorised plan changed, uses up
   a round limit, or you can already see the next work would fail the A4
   gate. Set `## Status` to say whether this part finished (`stopped
   unfinished` if not), `## Next` to `blocked: <reason>`, label the PR `plan:blocked`
   and remove any `plan:continue` added in step 3 (the labels always follow
   the final `## Next`), and tell the user, with ways forward. A PR
   whose own work is finished can still carry `plan:blocked`; it merges
   normally, and its merge starts no session — say so rather than promising
   one.

The session ends once ready is requested and any CI rounds it can act on
are done, or it has stopped. The PR's merge starts the session after.

---

## Part B — Ordinary session

### B1. Confirm the merge

As in A1. Not merged → say so in one line, record nothing as done, stop.
`gh` unavailable → say the merge is unverified and mark the record so.

### B2. Decide what is worth keeping

- **Already durable — point, don't copy:** the diff, commit messages, the PR
  body, review threads, decision records.
- **Would be lost — record it:** reasons not written anywhere else, traps
  and how they were avoided, things deliberately left out and where they
  went, follow-ups promised.
- **Noise — drop it:** review-round blow-by-blow, command output, retries.

Name the **next task** from the plan or the conversation: what it is,
whether the user has authorised it (quote their words), and its starting
state (repository, branch or worktree, base commit, files). If nothing is
queued, say "none" rather than inventing one.

### B3. Update memory and the plan

- **Memory** (if the session has one): update the existing memory for this
  work stream in place — mark the PR merged (number, SHA, absolute date),
  add the lessons, rewrite the status line, and refresh the index hook. One
  "next" line, never three. Never copy credentials or private data.
- **Plan** (if the session has one; do not invent one): one `## Next` at
  the top; the finished item collapses to
  `DONE <date> — <what changed> — PR <link> (<sha>)`; stale breadcrumbs go.
  A plan committed to a repository is a repository change: follow that
  project's rules for it.

### B4. Hand over

Do the tidy step (A6), then end in the first way that applies:

1. **Started by a parent session:** report and stop.
2. **Desktop app:** once the records are saved, offer to clear the session
   (`clear_session` with `"self"`); the next turn starts from the plan's
   `## Next`.
3. **Otherwise:** a paste-ready `/compact` line. Its focus text starts with
   `Focus on <next task>.`, then the authorisation (quoted, or "not yet
   authorised"), the starting state with absolute paths, constraints not
   already in always-loaded files, pointers to the plan and memory file,
   and one clause on the finished work. Under about 1,500 characters;
   longer means finished work is leaking in. No next task → `/compact`
   naming the memory file and plan, and "no task in progress".

```text
Recorded PR <n> (merged <date>, <sha>): memory `<file>` updated, plan pruned (Next: <task>).

Paste this:
/compact Focus on <next task>. ...
```

Then stop.
