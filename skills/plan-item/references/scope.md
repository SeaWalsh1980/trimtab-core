# The item's scope and the fix limits

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Step 3 of plan-item/SKILL.md; plan-pr/SKILL.md uses the same definitions for CI findings and
checks. Section numbers in this file are plan-run/SKILL.md's.

## Within the item's scope

This decides which findings the run fixes and which it records and leaves. It is a fact about the
diff, never the run's judgement of what the item "is about". The merge-base is the branch's
merge-base with the default branch (`git merge-base origin/<default> HEAD`), taken again after any
merge of the default branch into the branch.

| The finding or check | In scope? |
|---|---|
| A finding on a line the branch's diff from the merge-base changes | Yes |
| A finding the merge-base does not have on any line (the item caused it, wherever it shows) | Yes |
| Any other finding, including an old one elsewhere in a file the item touched | No |
| A failing check that passes on the merge-base | Yes |
| A failing check that also fails on the merge-base | No |

An in-scope finding is fixed, whatever its severity. An out-of-scope finding becomes its own
register entry (plan-item/references/records.md), unless an entry already records it; one graded
CRITICAL or HIGH is also a stop. This scope is separate from the write scope (section 4's third
condition): a fix must also stay inside the plan's write scope.

## The fix limits

They cover every change the run makes: the item's own work, and every fix for an in-session
round's finding, a CI review's finding or a failing check.

- **No suppression or skip marker** is added: no `noqa`, `type: ignore`, `nosec`, `pragma`, skip
  decorator, `xfail`, lint-disable comment, scanner exemption, or the like from the project's list.
- **No test, check or threshold is weakened or removed:** no deleted or emptied test, loosened
  assertion, lowered coverage or severity threshold, or check taken out of a workflow or a
  configuration.
- The one exception for both: the authorised plan's item names that marker, test, check or
  threshold.
- **A secret-scan failure is never fixed by the run.** It is a stop, whatever it found. The only
  push allowed after it is one touching only the part's progress record.

The guard refuses a push that breaks a limit (E18, E19). If it refuses, do not route around it: a
fix that needs the limit broken is not made, and the finding stays open as a stop or an entry.
