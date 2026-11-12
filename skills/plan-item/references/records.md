# Progress records and register entries

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Step 2 of plan-item/SKILL.md. `<plans>` and `<plan>` are as plan-run/SKILL.md defines them.

## The progress record

**Before requesting ready**, add `<plans>/progress/<plan>/item-NN-partK.md`:

```markdown
# <plan> — item NN, part K

## Done
<what this PR did; decisions and why; lessons; links to issues logged>

## Status
item NN complete | item NN part K complete; item continues |
item NN part K stopped unfinished

## Next
item NN part Y: <brief a fresh session can start from — scope, files,
starting state> | item MM part 1: <brief> | plan complete |
blocked: <reason>
```

and record anything you found but did not do as new files in `<plans>/unauthorised/<plan>/`, one per
entry, named `item-NN-partK-MM.md`, linked from `## Done`. MM is the next number not already used on
this PR's branch (from `01`): a session resuming an in-flight PR continues after the entries already
there, and never writes over one, even though the guard would still allow it while the file is new
on the branch. The item and part in the name keep two PRs from colliding.

```text
item: NN part K
pr: <link>
date: YYYY-MM-DD
status: open
issue: <one paragraph>
```

Never change or remove an existing entry, its status included: entries are create-only, and statuses
are the operator's alone.
