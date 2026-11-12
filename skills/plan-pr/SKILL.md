---
name: plan-pr
description: Take a plan item's draft PR to merge — wait for the CI review the draft starts, reply to every thread with an outcome tag, CI fix rounds with one in-session round each, the named re-request, when to mark it ready or return it to draft, the watch to merge, and who may merge it. Use once a plan item's draft is open, and in a session started for an item PR it did not open; not for PRs outside an authorised plan, and not for the branch, record and in-session rounds before the draft (plan-item).
---

# Take a plan item's PR to merge

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

This is step 7 of plan-item/SKILL.md. Section numbers in this file are plan-run/SKILL.md's; step
numbers are this file's. Every stop follows plan-run/references/stops.md.

## Who owns the PR

The session that opened the draft owns the PR until it merges or a stop applies. When that session
ends first, a session started for the PR (by the operator, or a routine on review events as a
fallback) takes ownership after running plan-run's sections 2–4 for the PR's item:

| The new owner | It may |
|---|---|
| Holds the part's round ledger (same machine and config dir) | Everything in this skill. |
| Holds no ledger, and the PR is ready | Only watch: plan-pr/references/watch.md, "Watch-only". |
| Holds no ledger, and the PR is a draft | Nothing: a stop. |

## 1. Wait for the CI review

Opening the draft starts the CI review. Wait for its verdict and threads, within the bound the
project's instruction file names, or 60 minutes per review where it names none. Which review counts,
how to read it, and what a verdict means: plan-pr/references/ci-review.md.

| What arrives within the bound | Then |
|---|---|
| A readable verdict | Step 2. |
| A verdict that failed closed before reaching any finding | Request it again once, through the named mechanism (step 3). That is not a round. A second on the same head is a stop. |
| No CI review on the draft, or no readable verdict | A stop. |

## 2. Reply to every thread

Every thread any CI review run posted on the PR gets a reply from you opening with the project's
outcome tag for what you did. Which tag, for which finding: the table in
plan-pr/references/ci-review.md. A CRITICAL or HIGH thread takes only the fixed tag; anything else
there is a stop. An out-of-scope finding becomes a register entry and gets the deferral tag and the
entry's path. Scope is decided only as plan-item/references/scope.md says.

## 3. CI fix rounds

When a verdict or thread needs a fix within scope:

1. Fix every in-scope finding, within the fix limits (plan-item/references/scope.md), and push.
2. Run the one in-session round on the new head and act on it as plan-item/references/rounds.md
   says for the round after a CI fix.
3. Record both in the progress record, and reply to the threads (step 2).
4. Request a re-review of the new head, only through the mechanism the project's instruction file
   names and the guards permit, never one that marks the PR ready or toggles it. Re-request only a
   new head (the one fail-closed re-request apart). Where none is named, or the guard refuses it,
   that is a stop.
5. Back to step 1.

**At most three CI fix rounds per item part**, counted across every PR and branch the part has had,
from server facts (E12). One round is the push after the draft opened that answers a CI review or a
failing check, together with the push that fixes its in-session round. Not rounds: the review the
draft's opening starts, the fail-closed re-request, and the register-only commit (step 4). An
infrastructure failure is re-run once and does not count; where your token may not re-run it, name
it for the operator in a PR comment. A fourth round needed is a stop.

## 4. Ready

**Mark the PR ready** only when all of these hold, through what the guards permit (`gh pr ready`,
or `update_pull_request` with `draft` false where `gh pr` is blocked):

- no CI verdict on the head reports a CRITICAL or HIGH finding, in scope or out;
- every thread any CI review run posted on the PR has your reply opening with an outcome tag;
- every MEDIUM or LOW finding the in-session rounds left is a register entry in the PR;
- the local checks pass.

**Ready describes one head.** Return the PR to draft before any further commit (`gh pr ready
--undo`, or `update_pull_request` with `draft` true where `gh pr` is blocked), and mark it ready
again only once a CI review of the new head, run while it is a draft, meets the list above. The one
exception is the commit that only adds register entries for the last CI review's out-of-scope
findings and the progress record's links to them, touching nothing outside the plan's register
folder and this PR's progress record: it needs no further review, and the verdict on the commit it
follows stands for it. If a guard refuses, stop.

## 5. Watch to merge

After ready, watch every check you can read on the PR's head, and every CI thread, until the PR
merges or a stop applies, within the bound the project's instruction file names, or 24 hours from
ready where it names none. What each event means — a failing check in or out of scope, a check you
cannot read, a new thread, a conflict with the default branch, the bound running out — is the table
in plan-pr/references/watch.md. Whatever needs a commit goes back to draft first.

## 6. The merge

The item PR needs an approving review from the approver or the admin on its final head commit, and
is merged by the admin, or by the approver when every condition in plan-run/references/record-authority.md
holds. If you cannot read it, the record authorises nothing: stop. The agent account never merges.
When it merges, plan-run/SKILL.md takes over.
