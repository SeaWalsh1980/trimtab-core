---
name: wrap-merged
description: Close out a merged pull request and move to what comes next. In a repository with an authorised plan (`docs/plans/authorised/`), verify the plan, work out the next item from the merged progress record, and hand over to a fresh session, or stop on a blocker. In any other session, record the finished work in memory and the plan and hand over a clean context. Use whenever a PR is reported merged ("merged", "it landed", "PR is in"), when a session starts from a merge or a routine ("continue the plan", "next plan item"), when you are about to open the PR for a plan item, and when the user asks to wrap up or get ready for the next task — even if they never mention plans, memory or compaction.
---

# Wrap up a merged PR

When a PR merges, its history belongs in durable records and the live
session should carry only what comes next. Leftover review rounds, dead ends
and command output cost context and let stale facts steer new work.

There are two situations. Decide which one you are in first:

- **Plan run** — the repository has `docs/plans/authorised/`, and the merged
  PR (or the work about to start) belongs to one of those plans: its branch
  carries a progress record under `docs/plans/progress/`, or the session was
  started to continue a plan. Follow **Part A**.
- **Ordinary session** — anything else. Follow **Part B**.

`gh pr ...` subcommands use GraphQL, which some environments (the cloud
GitHub proxy) block. If one fails, use REST through `gh api` instead, for
example `gh api repos/{owner}/{repo}/pulls/{n}`. Do not treat a refused
command as permission to try other routes around a guard.

---

## Part A — Plan run

The plan is a contract the operator approved by merging it. Your job is to
verify that contract, find exactly what it says comes next, and either hand
over cleanly or stop and explain. You never widen it: a hand-over cannot
create work, and nothing found along the way gets done without a new plan.

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

The plan is authorised only if **every** line below holds. Any failure is a
stop: report which check failed and do nothing else.

1. **The plan is on the default branch:**
   `git fetch origin && git ls-tree origin/<default> -- docs/plans/authorised/<plan>.md`
2. **It has not changed since it was added** — exactly one commit touches it:
   `git log --format=%H origin/<default> -- docs/plans/authorised/<plan>.md`
   (one line). More than one means it was edited after approval.
3. **The PR that added it was merged by the admin account.** Find the PR for
   that commit (`gh api repos/{owner}/{repo}/commits/<sha>/pulls`) and read
   its `merged_by.login`. Compare it with the admin login in instance
   configuration (`plan_authority.admin_login` in the Trimtab instance's
   `instance.json`). Never take the admin login from the plan, a progress
   record, or the conversation. If it is not configured, the check fails.
4. **The enforcement exists:** `.claude/write-allowlist` exists, and
   `CODEOWNERS` covers `docs/plans/authorised/`. Without them the plan
   cannot be run, however it was merged.

Why this strict: the merge is the only authorisation an autonomous run has.
A plan that was edited later, merged by the agent's own account, or lacks
the guards that keep the run inside its allowlist authorises nothing.

### A3. Work out the state

Everything comes from the default branch and open PRs, never from memory:

- **In flight:** an open PR whose branch adds a file under
  `docs/plans/progress/<plan>/`. If one exists, the next session resumes it.
- **Next:** the `## Next` section of the **most recently merged** progress
  record (`docs/plans/progress/<plan>/item-NN-partK.md`; order by merge
  commit, `git log --diff-filter=A`). No records yet → item 01 part 1.
- **Done:** the `## Status` lines of the merged records.
- **Open issues:** entries with `status: open` in
  `docs/plans/unauthorised/<plan>.md` (same name as the plan).
- If the merged PR carries no progress record, report it as a gap.
- Sanity-check the record against the code: if it says an item is complete
  but the plan's done condition is plainly unmet on the default branch, log
  it in the register, and treat it as a blocker if the next work depends
  on it. A record is the agent's claim; the merged code is the fact.

### A4. Gate the next work

The next work may start only if all of these hold:

- `## Next` names an item that exists in the authorised plan and is not
  already complete. Anything else is a blocker — a hand-over cannot create
  work.
- It is not `blocked: <reason>` and not `plan complete`.
- Doing it would not need a write outside `.claude/write-allowlist` or to
  any always-refused path: security controls (workflows, `CODEOWNERS`,
  Claude Code settings and hooks, scanner exemption lists, repository
  settings), the harness (rules directory and `overrides.md`, the Trimtab
  lock, agent/command/skill definitions), or `docs/plans/authorised/`. Plan
  approval never covers these.

If the gate fails: say "needs approval: <reason>", list the open issues with
their PR links, and stop. If `## Next` is `plan complete`, say so and stop.

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

What matters is whether **this session's context holds the merged PR's
work**.

- **It does not** — the session started fresh from the merge: a routine or
  Action fired on it, a parent sent the brief, or a cleared session picked
  it up. The context is already clean, so if A4 passed, start the next work
  now, from `## Next`, and finish it with "Opening a plan item's PR" below.
- **It does** — this session did the merged work. Hand over instead, so the
  next work starts clean. The brief is the merged record's `## Next`, plus
  "run the authorisation check first". End in the first way that applies:
  1. **Started by a parent session:** report and stop. The parent clears
     this session and sends it the brief.
  2. **Desktop app:** tell the user the next turn starts from that `## Next`,
     then clear this session (`clear_session` with `"self"`), if the tool is
     available.
  3. **Otherwise (plain CLI):** give a paste-ready `/compact` line (see B4) —
     you cannot run `/compact` yourself.
  In the cloud there is nothing to hand over: the next merge, labelled
  `plan:continue`, starts the next session.

Before starting or handing over, report in a few lines:

```text
PR <n> merged <date> (<sha>) — plan <plan>, <Status line>.
Authorisation: ok. Next: <item NN part Y: one-line summary> | needs approval: <reason> | plan complete.
Open issues: <UI-NN (PR link), ...> | none.
```

When handing over, stop after the report: starting the next item in a
context that holds the finished work drags its history along, which is what
this skill exists to prevent. After the next item's PR is open, stop too —
its merge starts the session after.

### Opening a plan item's PR

When the work for an item (or part) is ready to become a PR, before you
open it:

1. Add `docs/plans/progress/<plan>/item-NN-partK.md` to the branch:

   ```markdown
   # <plan> — item NN, part K

   ## Done
   <what this PR did; decisions and why; lessons; links to issues logged>

   ## Status
   item NN complete | item NN part K complete; item continues

   ## Next
   item NN part Y: <brief a fresh session can start from — scope, files,
   starting state> | item MM part 1: <brief> | plan complete
   ```

2. Append anything you found but did not do to
   `docs/plans/unauthorised/<plan>.md` (create it if absent), one line each:
   `UI-NN | <date> | item NN | PR <link> | <issue> | status: open`. Never
   change an existing entry's status.
3. Open the PR as a draft. Label it `plan:continue` unless `## Next` is
   `plan complete` or `blocked: <reason>`.
4. Review it in session before marking it ready. It is ready when no
   CRITICAL or HIGH finding remains and the local checks pass. Fix findings
   inside the item's scope; log the rest in the register. At most three
   review rounds, then at most two rounds fixing CI review findings;
   infrastructure failures are re-run once and do not count.
5. If the run has to stop — a round limit is used up, a write was
   refused, the work needs something outside its scope, or you can already
   see the next work would fail the A4 gate — set `## Next` to
   `blocked: <reason>`, label the PR `plan:blocked` and not
   `plan:continue`, and tell the user. A PR whose own work is finished can
   still carry `plan:blocked`; it merges normally, and its merge starts no
   session — say so rather than promising a next session.

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
