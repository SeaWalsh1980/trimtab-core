---
name: plan-run
description: Run an authorised plan across merges — confirm the merge, run the authorisation check, work out the state, gate the next work, then continue, hand over or stop. Use when a plan PR is reported merged, when a routine or a `plan:continue` merge starts a session, when the operator restarts a stopped run, or when a session starts for an item PR's CI review; not for merges outside an authorised plan, and not for working an item (plan-item) or reviewing and readying its PR (plan-pr).
---

# Run an authorised plan

Paths such as `plan-run/references/stops.md` are relative to the skills directory, the parent of
this skill's own directory.

**If you cannot read a file this skill links to, stop.** Make no file write if check 0 failed, and
never edit a merged record. Otherwise, in the open item PR you are working on, set its record's
`## Status` to say whether this part finished (`stopped unfinished` if not) and `## Next` to
`blocked: <reason>`. Label that PR `plan:blocked` and remove any `plan:continue`; before an item PR
exists, label the merged PR that started the session instead, and a valid restart, which has no such
PR, labels nothing. Report the blocker with one to three ways forward. Where a link below says more
about an unreadable file, that adds to this rule.

When a plan PR merges, its history belongs in durable records and the next session should carry only
what comes next. Leftover review rounds, dead ends and command output cost context and let stale
facts steer new work.

Below, `<plans>` is the project's plans directory: the `plans` directory directly inside the
repository's top-level `docs` directory. An authorised-plan project keeps three folders there:
`<plans>/authorised/` (plans the operator merged, each `<plan>.md` with its write declaration
`<plan>.writes`; never edited), `<plans>/progress/<plan>/` (one record per PR) and
`<plans>/unauthorised/<plan>/` (the issues register, named like its plan, one create-only file per
entry).

**Session mode.** Every plan run is an autonomous plan run: trimtab-core's session-mode hook
announces it at session start, and the run is launched with `TRIMTAB_PLAN_RUN=1`. A run is always
narrower than an interactive session; read every step below that way.

**This skill applies only to plan runs:** the repository has `<plans>/authorised/`, and either the
merged PR adds a record under `<plans>/progress/`, adds a plan under `<plans>/authorised/`, or
carries a `plan:*` label, or the session was started to continue or restart a plan. A PR with a
`plan:*` label but no record is a plan PR missing its record: it is a stop (section 3). Any other
merge is outside this skill: say so in one line and do nothing here. In particular, never edit
anything under `<plans>/` for a merge that is not a plan run.

`gh pr ...` subcommands use GraphQL, which some environments (the cloud GitHub proxy) block. If one
fails, use REST through `gh api` instead, for example `gh api repos/{owner}/{repo}/pulls/{n}`. A
guard refusing a command is never a reason to find another route to the same thing.

---

## The run

The plan is a contract the operator approved by merging it. Your job is to verify that contract,
find exactly what it says comes next, and then continue, hand over, or stop and explain. You never
widen it: a hand-over cannot create work, nothing found along the way gets done without a new plan,
and you never edit an authorised plan or an earlier progress record.

Every stop follows plan-run/references/stops.md.

### 1. Confirm the merge

```bash
gh api repos/{owner}/{repo}/pulls/<n> --jq '{state, merged, merged_at, merge_commit_sha, base: .base.ref, labels: [.labels[].name]}'
```

- `merged` is true and `base` is the default branch → continue.
- Merged into any other branch → the work has not landed where the run reads its state; treat it as
  not merged.
- A valid restart (see "Restarting after a stop") has no merged PR: skip this step and go straight
  to section 2.
- Not merged (open, or closed unmerged) → nothing is done. Keep any lessons (memory if the session
  has one, otherwise in your report), record nothing as done, and stop. A wrongly recorded "done"
  sends the next session past work that never landed.

### 2. Run the authorisation check

The plan is authorised only if **every** check holds. Any failure is a stop: say which check failed
and start nothing.

0. **This session is a launched run:** the session-mode announcement says autonomous and its signals
   include `TRIMTAB_PLAN_RUN=1`. (With no announcement, check 4 fails anyway; still check
   `TRIMTAB_PLAN_RUN` in your own shell environment, because it decides how you stop.) Without the
   variable, make no file write at all, not even a progress record: label the PR (or, before any
   item PR exists, the merged PR that started the session) `plan:blocked`, remove any
   `plan:continue`, and report. A clear loses the session's autonomous record; only the variable
   survives it.

Then read plan-run/references/authorisation.md and run checks 1–4 from it before going on. If you
cannot read it, the check fails: stop.

### 3. Work out the state

Everything comes from the default branch and open PRs, never from memory:

- **In flight:** an open PR whose head branch is `plan/<plan>/item-*` — recognisable from the moment
  its draft opens, before any record exists. The branch name alone proves nothing: the PR's head
  must be in this same repository (not a fork) and its author must be the agent or admin account
  from instance configuration (`accounts.agent` from `trimtab instance --json`, and the admin login
  `check-plan-authorised` printed on its verdict line). A file naming no agent leaves only the
  admin. A `plan/…` PR that fails either test is a stop — report it; never resume or build on it.
  Otherwise it is resumed before anything else, but only through the gate in section 4, applied to
  the item its branch names. An open PR labelled `plan:blocked` is not resumed this way; only a
  valid restart picks it up.
- **Next:** the `## Next` section of the **most recently merged** record
  (`<plans>/progress/<plan>/item-NN-partK.md`). Which record that is: see
  plan-run/references/record-authority.md.
- **That record's PR must carry the code gate and be merged by the admin, or by the approver with
  every condition met.** Read plan-run/references/record-authority.md to check it. If you cannot
  read it, the record authorises nothing: stop and label that PR `plan:blocked`.
- **Done:** the `## Status` lines of the merged records.
- **Open issues:** the entry files in `<plans>/unauthorised/<plan>/` whose `status` is `open`.
- **The merged PR carries no record** → stop, unless it is the PR that added the authorised plan
  itself (the plan's first run: start item 01 part 1). This applies to a session started by a merge;
  a valid restart has no merged PR to check here (a fix PR the operator merged is not a plan PR) and
  goes straight to the restart rule. Every item PR must carry a record, and continuing from an older
  `## Next` risks redoing work.
- **Sanity-check the record against the code.** If it says an item is complete but the plan's done
  condition is plainly unmet on the default branch, log it in the register, and stop if the next
  work depends on it. A record is the agent's claim; the merged code is the fact.

### 4. Gate the next work

The next work — `## Next`, or the item an in-flight PR works on — may start only if all of these
hold:

The conditions are in plan-run/references/gate.md, and every one must hold. If you cannot read it,
the gate fails.

If the gate fails: say "needs approval: <reason>", list the open issues with their PR links, propose
ways forward, and stop. If `## Next` is `plan complete`, say so and stop.

A stop before any item PR exists: see plan-run/references/stops.md.

### 5. Record

- **No repository write after the merge.** The progress record shipped in the PR; editing it now
  would put unreviewed text on the default branch.
- Memory, if the session has one: a short line (plan, item, PR, merge SHA, date, what comes next).
  It is optional — the merged record is the source of truth, and cloud sessions have no memory at
  all.

### 6. Tidy

Follow the project's own after-merge instructions (removing the PR's worktree or local branch,
pulling the default branch). Do only what they say; never delete a branch or worktree holding
unmerged work.

Then, if this session did the merged work, remove the temporary files it made for it. Which files,
and which to leave: see plan-run/references/after-merge.md.

### 7. Continue or hand over

Report first, in a few lines:

```text
PR <n> merged <date> (<sha>) — plan <plan>, <Status line>.
Authorisation: ok. Next: <item NN part Y: one-line summary> | needs approval: <reason> | plan complete.
Open issues: <item-NN-partK-MM (PR link), ...> | none.
```

Then what matters is whether **this session's context holds the merged PR's work**.

- **It does not** — the session started fresh from the merge: a routine or Action fired on it, a
  parent sent the brief, or a cleared session picked it up. The context is already clean, so if
  steps 3 and 4 passed, do the next work now — the in-flight PR if there is one, otherwise `## Next`
  — following "Working a plan item" below. That is plan-item/SKILL.md.
- **It does** — this session did the merged work. Hand over as plan-run/references/after-merge.md
  says.

**A session started by the `plan:continue` label** first checks who added it
(`gh api repos/{owner}/{repo}/issues/<n>/events`, the `labeled` event for `plan:continue`). Unless
the agent, approver or admin account added it, stop at once. Those logins come only from instance
configuration: `accounts.agent` and `accounts.approver` from `trimtab instance --json`, and the
admin's from `trimtab check-plan-admin`. The label only starts a session; it never authorises: the
session still runs section 2 and reads only an authorised record.

**Restarting after a stop** takes the operator's act: a session they start for the plan (locally, or
the routine's run-now). A PR they merge to fix the blocker carries no record and no `plan:continue`,
so it starts nothing by itself. Whether a session counts as a restart, and what it works on: see
plan-run/references/restart.md. If you cannot read it, no session is a restart: the stop stands.

**A session started for an item PR's CI review** (by the operator, or a routine on review events)
fixes the CI findings within scope, as in step 4 of "Working a plan item" below, and reviews the new
head in step 4's one to three in-session rounds before marking the PR ready again. Count CI fix
rounds (at most two per PR) from the PR's own review history, not from this session alone. Step 4 is
plan-pr/SKILL.md. The scope rules (step 2) and stop conditions (step 5) are in plan-item/SKILL.md:
read it before fixing anything.
