# In-flight work

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Part of plan-run/SKILL.md section 3 ("In flight"). Section numbers in this file are
plan-run/SKILL.md's.

## What is in flight

Either of these, for this plan:

- **An open PR** whose head branch is `plan/<plan>/item-*`.
- **A pushed branch** `plan/<plan>/item-*` that has never had a PR: no PR of this repository, in any
  state, has it as its head.

A branch is named `plan/<plan>/item-NN-partK` from its first push, so it is recognisable before any
record or PR exists. The name alone proves nothing.

## Resumed or not

Run `trimtab check-inflight --repository <owner>/<name> --plan <plan>`, as a command of its own, and
act on its exit code. It reads the plan's PRs and its pushed branches from the server and decides
the whole table by itself; the logins come only from instance configuration, never from a PR. It
resumes a PR only if its head branch is exactly `plan/<plan>/item-NN-partK`, its head is in this
repository (never a fork), its author is the agent or the admin account (a file naming no agent
leaves only the admin), and any `plan:blocked` stop on it was cleared by the admin and the label is
gone. It resumes a branch with no PR only if its name has that shape and the plan's newest stop
anywhere was cleared or there was none. Do not read the PRs, their authors, their labels or the
branch list yourself.

| Exit | Then |
|---|---|
| 0, printing `none in flight` | Nothing is in flight. The next work is the latest authorised record's `## Next` (section 3). |
| 0, printing `resume pr <n> <branch> <sha>` or `resume branch <branch> <sha>` | The one thing to resume: that PR (or that branch, which has no PR), at that head. Check that it holds nothing but the plan run's own in-scope pushes (below), then resume it through the gate in section 4, applied to the item and part its branch names, keeping its part and its round counts. |
| 1 | A stop: report **every** problem the command names, and start no new part. It names more than one thing in flight (a run resumes one and cannot tell which), a fork head, an author that is neither the agent nor the admin, a branch or head of the wrong shape, and a `plan:blocked` stop that is not cleared. Only a valid restart resumes a stopped PR or branch: plan-run/references/restart.md. If you cannot read it, no session is a restart: the stop stands. |
| 2 | A read failed, a list was cut off at its bound, a reply was not the shape expected, or the fetch is stale: run `git fetch origin` once (adding `--unshallow` for a shallow clone) and run it once more. A second exit 2 is a stop. Never resume on a read you could not make. |

Exit 0 says only that the one thing is not refused by what the command reads. It does not say the
work is the plan run's own: that is the next section's.

## "Only the plan run's own in-scope pushes"

The guards enforce this (E13–E15 in trimtab-core ADR 0063); the session checks it too, so that it
stops before building on work it cannot vouch for. Until a tested `trimtab` command does the check,
read it from the API. If the PR or branch holds anything else, or any read here fails, stop and
report it:

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
