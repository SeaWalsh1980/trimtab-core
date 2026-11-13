# Progress records and register entries

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Step 4 of plan-item/SKILL.md. `<plans>` and `<plan>` are as plan-run/SKILL.md defines them.

## The progress record

Add `<plans>/progress/<plan>/item-NN-partK.md` on the branch, keep it current as you go, and have it
complete before the draft opens:

```markdown
# <plan> — item NN, part K

## Done
<what this PR did; decisions and why; lessons; links to issues logged>

Review rounds:
- round 1 (<head sha>): <n> in scope fixed; <entries written, or none>
- round 2 (<head sha>): no finding within scope; review ended
- CI fix round 1 (<head sha>): <what it answered>; in-session round: <outcome>

## Status
item NN complete | item NN part K complete; item continues |
item NN part K stopped unfinished

## Next
item NN part Y: <brief a fresh session can start from — scope, files,
starting state> | item MM part 1: <brief> | plan complete |
blocked: <reason>
```

Write each round's outcome as the round ends, in-session and after each CI fix: the head it
reviewed, what it fixed, and the entries it wrote. The lines are the run's account; the count that
binds is the guard's ledger. A check the run could not read, or a re-run its token may not trigger,
is also named here with the next push, if one is made.

The record carries exactly one `## Status` heading, at the start of a line, with exactly one line
under it, and no carriage return or indented heading anywhere in the file (`trimtab latest-record
--next-on-restart`, trimtab-core ADR 0071, reads it exactly as strictly as a plan's `## Items`
section). A second `## Status` heading anywhere — a quoted or fenced example included — makes that
command refuse the restart, which is a blocker no later session can clear, because AGW-4 bars
editing an earlier record. If `## Done` discusses the status line's own format, as this item's own
record may, write the example inline in backticks on one line or indented four spaces or a tab, never
as a `##`-prefixed line of its own.

## Register entries

Record anything you found but did not do as new files in `<plans>/unauthorised/<plan>/`, one per
entry, named `item-NN-partK-MM.md`, linked from `## Done`. MM is the next number not already used on
this part's branch (from `01`): a session resuming an in-flight branch or PR continues after the
entries already there, and never writes over one, even though the guard would still allow it while
the file is new on the branch. The item and part in the name keep two PRs from colliding.

```text
item: NN part K
pr: <link to the PR, or, before the PR exists, the branch name plan/<plan>/item-NN-partK>
date: YYYY-MM-DD
status: open
issue: <one paragraph>
```

An entry written before the PR exists carries the branch name in `pr`: it identifies the PR the
branch later opens, and is never rewritten to the PR's link.

Never change or remove an existing entry, its status included: entries are create-only, and statuses
are the operator's alone.
