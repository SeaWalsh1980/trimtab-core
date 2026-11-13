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
under it, directly followed by `## Next` (the section ends only at a `## ` heading, so any other
non-blank line before it, a `###` heading included, counts as a second line and is refused).
`trimtab latest-record --next-on-restart` (trimtab-core ADR 0071) reads the record exactly as
strictly as a plan's `## Items` section, and refuses it when any of these hold anywhere in the file:

- a second `## Status` heading, a fenced example included (the heading is matched without case, so
  a fenced `## status` counts too);
- a heading named Status in a form the reader does not recognise: `# Status`, `### Status`,
  `## Status ##`, `## *Status*`, the word `Status` alone (in emphasis, or behind a `>` blockquote
  marker) over a `---` or `===` underline, or any HTML heading tag (`<h1` to `<h6`);
- a carriage return that is not the last character of its line (a CRLF file reads normally);
- a line that begins with one or more spaces or tabs, then one to six `#` characters, then a space
  or tab, including an indented `#` comment inside a fenced code block, which the reader cannot tell
  from a heading;
- a line where one to six `#` characters and a space or tab come after a blockquote marker (`>`,
  nested included) or a list marker (`-`, `*`, `+`, `N.`, `N)`), with any indentation: `> ## Status`
  and `- ## Status` are refused, because markdown renders them as headings.

The template's own unindented `# <plan>` title and its `## Done`, `## Status` and `## Next` headings
are expected and are none of these. A refusal is a blocker no later session can clear, because AGW-4
bars editing an earlier record. If `## Done` discusses the status line's own format, as this item's
own record may, write the example only inline in backticks within a sentence, never as its own line:
not indented, not behind a blockquote or list marker, and not starting the line with `#`.

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
