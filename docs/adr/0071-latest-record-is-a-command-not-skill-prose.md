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
- Exit codes follow `check-plan-authorised`: 0 ok, 1 a named problem, 2 a failed read, a stale fetch
  (origin's default branch must be where the server says it is, which is why `--repository` is
  required), a shallow clone or a grafts file.
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

## Consequences

- `--repository` is required, beyond the usage first sketched, because the stale-fetch check needs
  the server's branch tip.
- `--next-on-restart` also needs the instance's admin login and runs the authorisation check, since
  the plan's item list is read through it. When the latest record is item-complete and the plan
  names no following item, it prints `plan complete`.
- The plan's item list is recognised by a pattern (`Item N`, a heading or checklist item, or a
  numbered line). A plan written in another shape reads as having no next item; the answer is
  `plan complete`, which the operator would notice. A fixed plan format is separate work.
- `planauth` gains two thin public wrappers around the checks it already made, so the stale-fetch
  rule stays in one place.
