# 0071. The latest progress record is found by a command, not by skill prose

## Status

Proposed (2026-11-13). Accepted when the PR that adds it merges. Changes
`skills/plan-run/references/record-authority.md` and `restart.md`. Item 4 of the seven-item plan
that ADR 0068 began (split the skill, sync it, then add commands for checks the model did by hand).

## Context

`plan-run` had the model find "the latest merged progress record" by running `git log` and reading
the output: newest merge first, a SHA line, then paths, with a tie-break when one commit added
several records. The model had to hold several rules at once (first-parent order, not commit dates;
highest item then part; no records means item 01 part 1; a shallow clone makes the oldest commit
read as adding every record), and a slip in any of them names the wrong record and so the wrong
next work. A restart had a second hand-worked rule: next work from the file name and `## Status`,
never the record's other text.

The commands `check-plan-authorised` and `check-plan-scope-job` already show the pattern: one
tested call, fixed exit codes, nothing for the model to interpret. This is a convenience that makes
the skill reliable. It enforces nothing; the guards do, and the record's PR is still checked by the
code gate and merger tests.

## Decision

Add `trimtab latest-record --repository <owner>/<name> --plan <plan> [--default-branch <name>]
[--next-on-restart] [--repo <path>]`, in `trimtab/planrecord.py`.

- It reads the fetched default branch's first-parent history with one `git log` call (the pattern
  `planscope.added_by` uses for one path) and prints one line: the record's path, the commit that
  added it, `item NN`, `part K`. No records prints `item 01 part 1`.
- A record name that is not `item-NN-partK.md` in the plan's own folder is a named problem (exit 1),
  never skipped.
- `--next-on-restart` takes the next work only from the record's file name and its `## Status` line:
  part K+1 for `complete; item continues` and `stopped unfinished`, the plan's next item (or `plan
  complete`) for `item NN complete`. The item list comes from `planauth.check`'s plan text, so the
  command never names the authorised plans folder. A `## Status` that is missing, in none of the
  three forms, or that names another item or part than the file name, is exit 1.
- The item list is one strict section, not a pattern over the whole plan. The plan holds exactly one
  `## Items` heading; its section runs to the next `## ` heading or the end of the file; every
  non-blank line in it is a top-level item `N. <text>`, numbered from 1 with no gap, no repeat and
  no reordering, or a line indented under an item (its wrapped text or its sub-steps, which are
  never items). A missing or repeated heading, an empty section, a line that is neither, or a number
  out of sequence is a named problem (exit 1). `plan complete` is printed only when the record's
  item is the section's last item.
- A sub-step is indented four spaces or a tab. A numbered line indented less, which markdown reads
  as an item of the same list, is a named problem rather than wrapped text: it is the one shape that
  could hide a later item and so end a plan early. The section ends only at a `## ` heading at the
  start of a line or at the end of the file, so any other unindented non-item line inside it — a
  `###` heading, a `#` title, a `---` rule, a code fence, a table row — is refused by name rather
  than read as content or as the section's end; a `## Items` line inside a code fence counts as a
  second heading and is refused too, as is a heading indented inside the section, which markdown may
  read as ending it. The heading itself is matched without case (`## items` reads), allowing trailing
  blanks, and lines are split on newlines alone with a trailing `\r` stripped, so a CRLF plan reads
  normally instead of reporting a missing section; a carriage return left *inside* a line is refused,
  because markdown reads it as a line ending and an item after it would be invisible here. That one
  check reads the whole plan rather than the section alone, which is the single exception to "nothing
  outside the section is read": a carriage return before the heading can hide a second `## Items`
  line, so the file has to be sound before the section can be located at all. Each of
  those refusals is loud, and the ones a real plan might hit are pinned by tests.
- The record's `## Status` is read exactly as strictly as the plan's `## Items`, and for the same
  reason: it is the least trusted input on the one path whose output becomes a run's next work. The
  heading counts only at the start of a line, matched without case and allowing trailing blanks — for
  the record, matching without case is itself the refusing choice, since a lower-case decoy is then
  counted as a second heading rather than passed over; a second `## Status` heading — including one quoted in a code fence, which a record about this change
  might well carry — is refused by name rather than letting the first win; the text is split on
  newlines alone, and a carriage return inside a line is refused. An indented heading anywhere in
  the record is refused too, since markdown may read it as a `## Status` this reader does not see;
  and a second non-blank line under the heading is refused, since the reader cannot tell which line
  is the status. One helper does the splitting for both readers so they cannot drift apart again.
- Both restart paths read the item list, not only the item-complete one. `item NN part K complete;
  item continues` and `stopped unfinished` print part K+1 only once the section names item NN, so a
  part of an item the plan does not have, or a plan whose section cannot be read, is refused rather
  than answered. The alternative — trusting the record alone, since the record proves the item ran —
  was rejected: it let the command print work for an item no authorised plan contains.
- Exit codes follow `check-plan-authorised`: 0 ok, 1 a named problem, 2 a failed read, a stale fetch
  (origin's default branch must be where the server says it is, which is why `--repository` is
  required), a shallow clone or a grafts file. One difference: a malformed plan name is exit 1
  here, since no fetch can fix it, where `check-plan-authorised` treats a bad argument as exit 2.
- The skill runs it as a command of its own with no pipe and acts on its exit code.

## Options considered

- **Keep it in skill prose.** Rejected: it is the state of affairs that prompted the plan. Every
  session re-derives the rule by hand and a wrong reading is silent.
- **Fold it into `check-record-authorised` (item 3).** Rejected: finding the record and judging its
  PR are separate questions with separate failure modes, and the restart path needs the first
  without the second. Item 3 uses this command's output (the commit SHA) rather than repeating it.
- **Pick the record by commit date or by highest number.** Rejected: dates inside a branch are
  arbitrary, and a later merge may legitimately carry a lower item number (a re-run part).
  First-parent order is merge order. Only a tie within one commit falls back to the numbers.
- **Read the item list by pattern, anywhere in the plan** (this PR's first three rounds: `Item N`,
  a heading or checklist item, a column-0 `N.`, then a second wider scan for `Step N`, `Task N` and
  indented numbers). Rejected after three review rounds each found another shape it missed
  (`**3.** c`, `+ Item 3`, `> 3.`, a table row, `Item #4`, `Phase 4`). A shape whitelist cannot
  prove that no later item exists, so every miss ended a plan early and silently, and every
  addition widened the opposite error, where a numbered sub-step or a preamble list stopped a
  finished plan or invented an item. A declared section is checkable: the plan either has it or the
  command refuses.
- **Not parsing the plan at all** — take the next item as `record.item + 1` and leave the end of the
  plan to the operator. Rejected: a run would then start an item the plan does not have, and
  `plan complete` could never be printed, so every plan would end as a blocker.

## Consequences

- `--repository` is required, beyond the usage first sketched, because the stale-fetch check needs
  the server's branch tip.
- `--next-on-restart` also needs the instance's admin login and runs the authorisation check, since
  the plan's item list is read through it. When the latest record is item-complete and the plan
  names no following item, it prints `plan complete`.
- **A plan written without a `## Items` section stops for the operator**, on the restart path only:
  `--next-on-restart` exits 1 naming what it could not read, and every other use of the command is
  unaffected. That is the accepted cost of the strict section, and it falls on the plans that exist
  today, none of which has one. The instance's rule for an authorised plan should state the format,
  so a plan that lacks it fails when it is written rather than at a restart; until it does, this
  record is the only statement of the format.
- **A record that trips the `## Status` refusal cannot be repaired in place either**, and for the
  same reason as the plan: AGW-4 bars editing an earlier progress record, so the stop falls on the
  restart path and is the operator's to clear, not a later session's. `skills/plan-item/references/
  records.md` now tells the author the constraint (one `## Status` heading, one line under it, no
  stray carriage return or indented heading) and how to discuss the format safely in `## Done`, so a
  new record need not hit this; an existing record that already trips it is fixed by the operator.
  The indented-heading check cannot tell an indented heading from an indented `#` comment inside a
  fenced code block, since both are `#`-prefixed lines reading like markdown headings once
  unindented; it is accepted rather than narrowed, because narrowing it to "looks like a heading"
  would have to guess, and this reader's whole premise is that it does not guess. A record that
  quotes code with an indented `#` comment must say so in prose instead, or use a language whose
  comment marker is not `#`.
- **An already-merged plan cannot be repaired in place**, and the cost of that is larger than the
  stop itself. `planauth`'s check 3 fails once a plan's text differs from the commit that merged it,
  and `--next-on-restart` runs that check, so adding the heading is what would make the plan
  unauthorised; the agent may not edit a plan at all. The compliant route is a new plan file and
  declaration under a new `NNNN-<slug>`, admin-merged, with the old plan marked superseded. Because
  the progress folder is keyed on the plan's name, the new plan's lookup finds no record and returns
  `item 01 part 1`, so a half-finished plan restarts at item 1 unless the operator carries its
  records across by hand. This project accepts that: no plan is mid-flight today, and the restart
  path is the only one affected. A plan that is mid-flight when the format lands is the operator's
  to migrate, and they will find the stop before any work is redone, because the command refuses
  rather than guessing.
- Text outside the section is never read, so a numbered list in the plan's preamble can no longer
  add an item, and a sub-step indented under an item can no longer stop a finished plan. Those two
  errors, which the earlier pattern had, are closed rather than narrowed.
- Wrapped text and sub-steps are allowed inside the section when indented under an item, so a plan
  may describe its items there. An unindented line that is not an item is refused, which also keeps
  a stray paragraph from reading as the section's end.
- `plan complete` and the first-run `item 01 part 1` are not authorisation verdicts. The command
  never checks who merged a record, and it checks the plan's merger only with `--next-on-restart`
  when a record exists (a first run skips that check); the skill says so.
- Exit 2 is answered by fetching once and running the command again; a second exit 2 is a blocker.
  This replaces the skill's earlier "a shallow clone is a stop" for the latest-record lookup,
  because the command now detects the cut history itself and a fetch fixes it.
- The plan and its `.writes` declaration must both exist on the default branch (exit 1 otherwise),
  so a mistyped plan name is not read as a plan that has not started. `--default-branch` must be the
  repository's default branch.
- A record added and since deleted is a named problem, not the latest record.
- `planauth` gains two thin public wrappers around the checks it already made, so the stale-fetch
  rule stays in one place.
