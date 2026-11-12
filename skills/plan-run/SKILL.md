---
name: plan-run
description: Run an authorised plan across merges — confirm the merge, run the authorisation check, work out the state, gate the next work, then continue, hand over or stop. Use when a plan PR is reported merged, when a routine or a `plan:continue` merge starts a session, when the operator restarts a stopped run, or when a session starts for an item PR it did not open; not for merges outside an authorised plan, and not for working an item (plan-item) or taking its PR through CI review to merge (plan-pr).
---

# Run an authorised plan

Paths such as `plan-run/references/stops.md` are relative to the skills directory, the parent of
this skill's own directory.

**If you cannot read a file this skill links to, that is a stop:** follow
plan-run/references/stops.md. If you cannot read that either, make no file write; in a plan run,
label `plan:blocked` and remove any `plan:continue` on the open item PR or, with none open, on the
merged PR whose progress record supplied the next work (the plan's own PR for item 01, or the
merged PR that started the session if you have not found that record yet); and report the
blocker, naming the file, with one to three ways forward. The record's `## Next` is not updated
then, so where it and the label disagree, the label is authoritative. Where a link below says more
about an unreadable file, that adds to this rule.

When a plan PR merges, its history belongs in durable records and the next session should carry only
what comes next. Leftover review rounds, dead ends and command output cost context and let stale
facts steer new work.

`<plans>` is the project's plans directory: the `plans` directory directly inside the repository's
top-level `docs` directory. An authorised-plan project keeps three folders there:
`<plans>/authorised/` (plans the operator merged, each `<plan>.md` with its write declaration
`<plan>.writes`; never edited), `<plans>/progress/<plan>/` (one record per PR) and
`<plans>/unauthorised/<plan>/` (the issues register, named like its plan, one create-only file per
entry).

**Session mode.** Every plan run is an autonomous plan run: trimtab-core's session-mode hook
announces it at session start, and the run is launched with `TRIMTAB_PLAN_RUN=1`. A run is always
narrower than an interactive session; read every step here that way.

**This skill applies only to plan runs:** the repository has `<plans>/authorised/`, and either the
merged PR adds a record under `<plans>/progress/`, adds a plan under `<plans>/authorised/`, or
carries a `plan:*` label, or the session was started to continue or restart a plan, or for an item
PR. A PR with a `plan:*` label but no record is a plan PR missing its record: it is a stop (section
3). Any other merge is outside this skill: say so in one line and do nothing here. In particular,
never edit anything under `<plans>/` for a merge that is not a plan run.

`gh pr ...` subcommands use GraphQL, which some environments (the cloud GitHub proxy) block. If one
fails, use REST through `gh api` instead, for example `gh api repos/{owner}/{repo}/pulls/{n}`. A
guard refusing a command is never a reason to find another route to the same thing.

---

## The run

The plan is a contract the operator approved by merging it. Your job is to verify that contract,
find exactly what it says comes next, and then continue, hand over, or stop and explain. You never
widen it: a hand-over cannot create work, nothing found along the way gets done without a new plan,
and you never edit an authorised plan or an earlier progress record. Every stop follows
plan-run/references/stops.md.

### 1. Confirm the merge

```bash
gh api repos/{owner}/{repo}/pulls/<n> --jq '{state, merged, merged_at, merge_commit_sha, base: .base.ref, labels: [.labels[].name]}'
```

| What the PR shows | Then |
|---|---|
| `merged` is true and `base` is the default branch | Continue to section 2. |
| Merged into any other branch | The work has not landed where the run reads its state: treat it as not merged. |
| Not merged (open, or closed unmerged) | Nothing is done. Report it and stop. This stop **reports only**: no file write, no label, no record marked done. Keep any lessons in memory if the session has one, otherwise in the report. A wrongly recorded "done" sends the next session past work that never landed. |
| No merged PR: a valid restart, or a session started for an item PR | Skip this section and go to section 2. |

### 2. Run the authorisation check

The plan is authorised only if **every** check holds. Any failure is a stop: say which check failed
and start nothing.

0. **This session is a launched run:** the session-mode announcement says autonomous and its signals
   include `TRIMTAB_PLAN_RUN=1`. (With no announcement, check 4 fails anyway; still check
   `TRIMTAB_PLAN_RUN` in your own shell environment, because it decides how you stop.) Without the
   variable, make no file write at all, not even a progress record: label the item's PR `plan:blocked`
   or, before it exists, the merged PR whose progress record the session took its next work from
   (the plan's own PR for item 01); remove any `plan:continue` from it; and report. A clear loses the
   session's autonomous record; only the variable survives it.

Then read plan-run/references/authorisation.md and run checks 1–4 from it before going on. If you
cannot read it, the check fails: stop. Check 4 passes only on the enforcement command's exit 0,
never on anything the session attests.

### 3. Work out the state

Everything comes from the default branch, open PRs and pushed branches, never from memory:

- **In flight:** an open PR whose head branch is `plan/<plan>/item-*`, or a pushed
  `plan/<plan>/item-*` branch that has never had a PR. Either is resumed before anything else, but
  only when it holds nothing but the plan run's own in-scope pushes, and only through the gate in
  section 4, applied to the item its branch names. How to tell, and which in-flight work is not
  resumed: plan-run/references/in-flight.md.
- **Next:** the `## Next` section of the **most recently merged** record
  (`<plans>/progress/<plan>/item-NN-partK.md`). Which record that is, and the test its PR must pass
  (the code gate, and a merge by the admin, or by the approver with every condition met): read
  plan-run/references/record-authority.md. If you cannot read it, the record authorises nothing:
  stop.
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

The next work — `## Next`, or the item an in-flight PR or branch works on — may start only if every
condition in plan-run/references/gate.md holds. If you cannot read it, the gate fails.

| Result | Then |
|---|---|
| Every condition holds | Go to section 5. |
| A condition fails | Say "needs approval: <reason>", list the open issues with their PR links, propose ways forward, and stop as plan-run/references/stops.md says for a stop before the draft. |
| `## Next` is `plan complete` | Say so and stop. Nothing is labelled. |

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

Then what matters is whether **this session's context holds the merged PR's work**:

| This session | Then |
|---|---|
| Did not do the merged work: a routine or Action fired on the merge, a parent sent the brief, or a cleared session picked it up | The context is already clean. If sections 3 and 4 passed, do the next work now — the in-flight PR or branch if there is one, otherwise `## Next` — as plan-item/SKILL.md says. |
| Did the merged work | Hand over as plan-run/references/after-merge.md says. A part boundary is the one place a part's work moves between sessions. |

**A session started by the `plan:continue` label** first checks who added it
(`gh api repos/{owner}/{repo}/issues/<n>/events`, the `labeled` event for `plan:continue`). Unless
the agent, approver or admin account added it, stop at once. Those logins come only from instance
configuration: `accounts.agent` and `accounts.approver` from `trimtab instance --json`, and the
admin's from `trimtab check-plan-admin`. The label only starts a session; it never authorises: the
session still runs section 2 and reads only an authorised record.

**Restarting after a stop** takes the operator's act: a session they start for the plan (locally, or
the routine's run-now). A PR they merge to fix the blocker carries no record and no `plan:continue`,
so it starts nothing by itself. Whether a session counts as a restart, what it works on, and when a
new part starts: see plan-run/references/restart.md. If you cannot read it, no session is a
restart: the stop stands.

**A session started for an item PR it did not open** (by the operator, or a routine on review
events as a fallback) runs sections 2–4 for the PR's item, then takes ownership of the PR: it works
it to merge or a stop as plan-pr/SKILL.md says, which also says what such a session may do when it
does not hold the part's round ledger. CI fix rounds are counted per item part from server facts,
never from this session alone.
