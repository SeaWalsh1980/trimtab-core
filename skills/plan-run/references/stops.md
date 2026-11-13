# Stops

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Every stop in a plan run follows this, whichever skill it happens in (plan-run, plan-item, plan-pr).
Section numbers in this file are plan-run/SKILL.md's.

## Every stop

**Every stop** reports the blocker and proposes one to three ways forward for the operator (for
example: approve a new plan covering it, make the guarded change by hand, or correct the record in a
reviewed PR). Then, by where the part is; take the first row that applies:

| Where the part is | Record | Labels |
|---|---|---|
| The merge was not confirmed (section 1) | No file write. | None: that stop reports only. |
| One of the "Stops with no write" | No file write. | `plan:blocked` on the open item PR if there is one, otherwise on the record PR; remove `plan:continue` from it. |
| No branch pushed yet: any stop before the part's first push (sections 2–4, or plan-item step 1) | No file write. | `plan:blocked` on the record PR (below), through `gh api` if `gh pr` is blocked; remove `plan:continue` from it. |
| Work began on a `plan/…` branch, its draft has not opened, and this session may write to it | Commit the progress record on the branch, with `## Status` saying whether this part finished (`stopped unfinished` if not) and `## Next` reading `blocked: <reason>`, and push it, within the limits of "Stops with a limited write" where one applies. | The same merged PR as the row above: `plan:blocked` on, `plan:continue` off. |
| The item's PR is open | As the row above, pushed to the PR's branch (returning the PR to draft first if it is ready). | `plan:blocked` on the PR; remove any `plan:continue` from it. The labels always follow the final `## Next`. |

**The record PR** is the merged PR whose progress record the session took, or would take, its next
work from: the PR that added the latest record, found as the first part of
plan-run/references/record-authority.md says, a read of the default branch that needs no check to
have passed. If you cannot read it, the record authorises nothing: stop, and label as the fallback
below says. With no record yet, it is the plan's own PR (item 01). If the session stops before
it could find that PR, the merged PR that started the session; a restart with none of these labels
nothing and says so. A restart that resumed a branch or an open PR takes that branch's or PR's row.

**If a label write fails**, try it once more. If it fails again, make no further write, and report
loudly that the run is stopped but unlabelled, naming the PR, so the operator labels it.

## Stops with no write

These take the table's second row: no file write at all, not even the record; they only label.

- **Launched without `TRIMTAB_PLAN_RUN=1`** (check 0).
- **The authorisation check (checks 1–4) or the gate failed**, in any session, whatever is in
  flight: the run has no authority to write.
- **In-flight work the run may not build on** (plan-run/references/in-flight.md): a fork head, a
  foreign author, a push the run cannot vouch for, or a read that failed.
- **No round ledger for the part** (plan-pr/SKILL.md, "Who owns the PR"), whether the PR is ready
  (watch-only) or a draft. A ledger the session cannot read counts as none.

## Stops with a limited write

These take the row for where the part is, and write the stop record only:

- **A secret-scan failure:** never fixed by the run. The one write is one push touching only the
  part's progress record.
- **A stray Bash write** (a path the call changed outside the plan's scope, reported after the
  call): do not commit the stray write; the record's `blocked:` reason in `## Next` names its path.

## When the label and the record disagree

The label is authoritative. A stop that could not write its record (an unreadable stops file, a
refused write, a forbidden one) leaves `## Next` still naming work while `plan:blocked` is on. A
session reads the label first: a part, PR or record PR carrying `plan:blocked` stays stopped until
a valid restart, whatever its `## Next` says.

## A finished PR that stopped

A PR whose own work is finished can still carry `plan:blocked`; it merges normally, and its merge
starts no session — say so rather than promising one.
