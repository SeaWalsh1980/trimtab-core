---
name: wrap-merged
description: Close out a finished piece of work once its pull request is merged into the default branch — record what was done in memory and the session's plan, then hand the user a paste-ready /compact command that keeps only what the next task needs. Use whenever the user says a PR is merged ("merged", "merged into main", "it landed", "PR is in"), whenever you have just merged one yourself or seen it reported merged, and when the user asks to wrap up, close out, or get the session ready for the next task — even if they never mention compaction or memory.
---

# Wrap up a merged PR

A long session that ships one PR after another carries every finished PR's
detail into the next task: review rounds, dead ends, command output. That
costs context and invites stale facts to steer new work. When a PR merges, its
history belongs in durable records, and the live session should carry only the
next task.

This skill does three things, in order, and then stops:

1. **Records** what the merged work was, in memory and in the plan.
2. **Prunes** the plan so the next task is what a reader sees first.
3. **Hands over** a `/compact` command focused on the next task.

You cannot run `/compact` yourself — it is a built-in command only the user
can type. Your last output is the command, ready to paste, and nothing after it.

## 1. Confirm the merge

Check the PR on the server before recording anything as done:

```bash
gh pr view <n> --json number,title,state,mergedAt,mergeCommit,baseRefName,url
```

- `state` is `MERGED` → continue.
- Anything else (open, closed unmerged, merge queued) → say so in one line,
  record nothing as done, and stop. A wrongly recorded "done" sends the next
  session past work that never landed.
- No PR number in sight → take it from the conversation, or `gh pr list
  --state merged --author @me --limit 5`; ask only if it is still ambiguous.
- `gh` unavailable → say the merge is unverified and mark it so in the record.

Note the merge commit SHA and date: the records use them.

## 2. Decide what is worth keeping

Sort what the session knows about the finished work:

- **Already durable — point, don't copy:** the diff, commit messages, the PR
  body, review threads, decision records (ADRs). A link or a number is enough.
- **Would be lost — record it:** why a choice was made when the reason is not
  in the PR or an ADR; traps hit and how they were avoided; things deliberately
  left out and where they went instead; follow-ups promised; anything the next
  task depends on that lives only in this conversation.
- **Noise — drop it:** review-round blow-by-blow, command output, retries,
  hypotheses that went nowhere.

Then name the **next task**, from the plan's order or the conversation:
what it is, whether the user has authorised it (quote their words), and the
concrete starting state — repository, branch or worktree, base commit,
files involved. If nothing is queued, the next task is "none" — say so rather
than inventing one.

## 3. Update memory

Use the memory system the session already has (its instructions say where
memories live and their format). Most sessions do:

- **Find the existing memory for this work stream** — read the index (e.g.
  `MEMORY.md`) first. Update that file in place; do not create a second file
  for the same stream. Create a new one only if none covers it.
- In it: mark the PR merged (number, merge SHA, date), add the lessons from
  step 2, and set what comes next. Rewrite the status line rather than
  appending another — a memory with three conflicting "next" lines misleads.
- Update the index line's hook so it reads true now (e.g. "A + B merged; C next").
- Use absolute dates. Never copy credential values, tokens or private data
  into memory.

If the session has no memory system, skip this step and say so.

## 4. Update and prune the plan

Find the session's planning document: a plan file named in the session's
context, or one the conversation has been updating. If none exists, do not
invent one; say so.

Edit it so a cold reader gets the current state in the first screen:

- **`## Next` at the top**: the next task, its authorisation status, starting
  state, and the one or two constraints that bind it. Replace the previous
  `Next` — keep exactly one.
- **The finished item collapses to one line** in place:
  `DONE <date> — <what changed, in a clause> — PR <link> (<merge sha>)`.
  Its working notes go: the PR and the memory hold what matters.
- **Remove stale breadcrumbs** — scattered "NEXT:", "OLD NEXT:", superseded
  todo states. Leave open items, decisions that still bind, and other
  streams' sections alone; prune only what this merge made stale.

If the plan is a file committed to a repository, editing it is a repository
change: follow that project's rules (worktree, PR) or leave it and say so.
If a shell command that names the file is refused by a guard, use the
dedicated file-edit tool instead; do not try another route around a refusal.

## 5. Tidy what the merge left behind

Follow the project's own instructions for after a merge (for example removing
the PR's worktree and local branch, or pulling the default branch). Do only
what they say; for anything else, list it in one line and leave it. Never
delete a branch or worktree that holds unmerged work.

## 6. Hand over the compact command

Write one `/compact` command whose focus text lets a fresh context start the
next task without reading back the finished one. Include:

- `Focus on <next task>.` first.
- The authorisation, quoted as the user gave it, or "not yet authorised".
- Starting state: absolute paths, branch/worktree, base commit, PR numbers.
- Constraints for the next task that are **not** already in the always-loaded
  instruction files or memory (those reload by themselves).
- Pointers: the plan file path and the memory file name.
- One clause on the finished work: "PR <n> merged <date> (<sha>), recorded in
  <memory file>." No more.

Leave out the finished PR's review rounds, findings, test counts and command
history. Aim for under about 1,500 characters; a longer focus text is a sign
finished work is leaking in.

If no next task exists, write `/compact` with a focus naming the memory file
and plan, and "no task in progress".

## Output

Keep the report to a few lines, then the command in its own fenced block so
it can be copied whole:

````markdown
Recorded PR <n> (merged <date>, <sha>): memory `<file>` updated, plan pruned (Next: <task>). <Tidy line, if any.>

Paste this:

```text
/compact Focus on <next task>. ...
```
````

Then stop. Starting the next task before the user compacts drags the finished
work's context along with it, which is the thing this skill exists to prevent.
