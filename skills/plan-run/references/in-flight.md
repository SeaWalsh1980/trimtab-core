# In-flight work

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Part of plan-run/SKILL.md section 3 ("In flight"). Section numbers in this file are
plan-run/SKILL.md's.

## What is in flight

Either of these, for this plan:

- **An open PR** whose head branch is `plan/<plan>/item-*`.
- **A pushed branch** `plan/<plan>/item-*` that has never had a PR: no PR in any state has it as
  its head (`gh api 'repos/{owner}/{repo}/pulls?state=all&head=<owner>:<branch>'`).

A branch is named `plan/<plan>/item-NN-partK` from its first push, so it is recognisable before any
record or PR exists. The name alone proves nothing.

## Resumed or not

Take the first row that applies:

| What you find | Then |
|---|---|
| Any read this table needs fails (the PR, its author, its labels, the label events, the branch's updates) | Stop and report it. Never resume on a read you could not make. |
| The PR's head is in a fork, or its author is neither the agent nor the admin account (`accounts.agent` from `trimtab instance --json`, and the admin login `check-plan-authorised` printed on its verdict line; a file naming no agent leaves only the admin) | Stop and report it. Never resume or build on it. |
| An open PR with a `plan:blocked` label event and no later removal by the admin (read from its label events, never from the label's presence alone) | A stop: report it, and start no new part for its item. Only a valid restart resumes it: plan-run/references/restart.md. If you cannot read it, no session is a restart: the stop stands. |
| A branch with no PR, while the plan's most recent `plan:blocked` label event has no later removal by the admin (`trimtab check-label-actor --repository <owner>/<name> --restart --plan <plan>` judges that event. Exit 0: the stop was validly cleared, so this row does not apply. Exit 1 whose only problem starts `nothing to restart`: the plan has no stop, so this row does not apply. Any other exit 1 is a stop; on exit 2 run `git fetch origin` once and run it once more, and a second exit 2 is a stop) | A stop, as in the row above; only a valid restart resumes it. |
| The PR or branch holds anything but the plan run's own in-scope pushes (below), or any read below fails | Stop and report it. |
| Otherwise | Resume it, through the gate in section 4, applied to the item and part its branch names, and keep its part and its round counts. |

## "Only the plan run's own in-scope pushes"

The guards enforce this (E13–E15 in trimtab-core ADR 0063); the session checks it too, so that it
stops before building on work it cannot vouch for. Until a tested `trimtab` command does the check,
read it from the API:

- **Every commit on the branch that is not on the default branch** reached it through an unbroken
  chain of branch-update events ending at the current head
  (`gh api 'repos/{owner}/{repo}/activity?ref=refs/heads/<branch>' --paginate`), and **every
  update is the agent account's**. A gap in the chain, a truncated or expired listing, or a failed
  read is a stop.
- **No update rewrote the branch:** each is a fast-forward of the one before it.
- **The diff from the merge-base is inside the plan's scope:** the scope condition in
  plan-run/references/gate.md. If you cannot read it, the gate fails.
- **Each push was made by a plan run.** The session cannot see another session's environment:
  guard-github refuses any update to a `plan/*` ref without `TRIMTAB_PLAN_RUN=1` (E14), and the
  part's round ledger, which only hooks write, records it. Without that ledger on this machine and
  config dir, a part whose PR is not yet ready cannot be resumed: stop. After ready, a session
  without it may only watch (plan-pr/SKILL.md).

A push by anyone else, including the owner's "Update branch" button, blocks resume of that branch.
The part then continues on a fresh branch only after the operator deletes this one: "A new part"
in plan-run/references/restart.md. If you cannot read it, no session is a restart: the stop stands.
