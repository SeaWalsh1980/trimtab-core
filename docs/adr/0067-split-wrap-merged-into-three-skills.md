# 0067. Split `wrap-merged` into three skills

## Status

Proposed. Replaces `skills/wrap-merged/` with `skills/plan-run/`, `skills/plan-item/` and
`skills/plan-pr/`, each with on-demand `references/` files. Every moved procedure keeps its text
and behaviour. One behaviour is added, and only because the text is now split across files: what a
run does when it cannot read a file a skill links to (see Decision).
Earlier records that name `wrap-merged` (0042, 0043, 0048–0050, 0052, 0056, 0058–0060, 0062, 0063)
stand as written: the procedures they changed now live under the new names. Where 0063 plans
changes to `wrap-merged` (sizing parts to one session, the watch step), they land in the `plan-*`
skills.

## Context

A review of `skills/wrap-merged/SKILL.md` (518 lines) found it overloaded. One skill ran five
jobs: the plan loop across merges, the work on one item, the in-session review rounds and ready
rules, the session started for an item PR's CI review, and restarts after a stop. Its prose was
dense and stacked exceptions, so a reader had to hold the whole file to apply any one part. Its
description was stuffed with trigger phrases so that every one of those situations would load it,
and every one of them then loaded all 518 lines.

The operator decided the shape: three skills plus references, new names, and no alias. Bringing
the text up to date with the newer run flow (draft after the rounds, CI review on the draft, the
run watching its PR to merge) is a separate, later PR, so this one moves the text without
rewriting it, and can be checked sentence by sentence.

No measurement drove this beyond the line count; the finding was readability.

## Decision

- **`plan-run`** is the entry: whether this is a plan run, confirming the merge, the authorisation
  check, working out the state, gating the next work, recording, tidying, continuing or handing
  over, the `plan:continue` actor check, restarts, and a pointer for CI review sessions. Its
  references hold checks 1–4 (`authorisation.md`), the latest record and its PR's code gate and
  merger (`record-authority.md`), the gate's conditions (`gate.md`), restarts (`restart.md`), the
  stop protocol every skill shares (`stops.md`), and scratch clean-up and the hand-over
  (`after-merge.md`).
- **`plan-item`** works one item or part in one PR: branch and draft, record and register, scope,
  scratch, labels and when to stop. Its reference holds the record and register-entry templates
  and naming (`records.md`).
- **`plan-pr`** holds today's step 4: in-session rounds, ready, CI fix rounds and the merge gate.
- Sentences move verbatim. Text is reflowed to 100 columns. A sentence-level comparison against
  the old file shows no sentence lost and none duplicated. New text is:
  - frontmatter, headings, one-line cross-links, and the connective sentences a moved block needs
    (for example, that section numbers in `plan-item` are `plan-run`'s);
  - a line at the top of every file saying what its paths are relative to;
  - **the added behaviour:** one shared rule at the top of each SKILL.md for a linked file the run
    cannot read. It stops; makes no file write unless the session passed check 0, and never edits
    a merged record; otherwise sets the open item PR's record to `stopped unfinished` /
    `blocked:`; labels `plan:blocked` and removes `plan:continue` on the open item PR, or on the
    merged PR that started the session, or on nothing in a valid restart; and reports with one to
    three ways forward. Each clause restates a moved rule (check 0, section 5, `stops.md`, the
    ready rule); the trigger, an unreadable linked file, is the new part;
  - for a link whose file decides something, its own meaning when unreadable: the authorisation
    check fails, the record authorises nothing, the gate fails, no session is a restart.
- `wrap-merged` is deleted, with no alias.

## Options considered

| Option | Why it lost |
|---|---|
| One skill as a short router plus references | Keeps one description for five situations, so it still triggers on all of them and still loads the router each time; item and PR work, which have their own triggers, stay hidden behind it |
| Two skills, entry and item | Leaves review rounds, ready and the merge gate inside the item skill; those are the part the coming sync PR rewrites, and a CI review session needs them without the rest of the item workflow |
| Keep `wrap-merged` as an alias | An alias is a fourth skill whose description competes with the entry's for the same triggers, and it must be kept in step with three others. The name also describes only the after-merge moment, not a run |
| Keep `wrap-merged` as the entry's name | Same misleading name; consumers would still need to learn the two new ones, so it saves no migration |
| Rewrite the text while splitting | Mixes a move with behaviour changes, so neither can be reviewed on its own; the sync PR does the rewrite |
| No fallback for an unreadable link (rely on the reader) | A link the run fails to follow silently loses that part's rules, and the run carries on; reviews of this PR found it for checks 1–4, the gate, the record check, restarts and the stop protocol |
| A fallback copied next to each link (tried in review rounds 1–4, then replaced) | Each copy restated part of the stop protocol and drifted: the copies dropped `## Status` and the ways forward, could read as a write after the merge or before check 0, and named a PR a restart doesn't have. Every new link needed another copy, and each review round found one missing |

## Consequences

- Consumers that name `wrap-merged` in routine prompts, instance docs or instructions must change
  to `plan-run` (and `plan-item` / `plan-pr` where they mean those parts) when they move their pin
  to a base that carries this change. Until then their pin keeps the old skill.
- `bootstrap.sh` links each directory under `skills/` by name (ADR 0050), so the three skills are
  linked with no installer change, and a machine re-bootstrapped onto this base loses the
  `wrap-merged` link.
- A plan run now reads more than one file. A link it fails to follow loses that part's rules. So
  each skill says what its paths are relative to and carries the shared rule above.
  `tests/trimtab/test_skills.py`
  fails when a link names no file or no skill, a reference can't be reached from a SKILL.md, a
  SKILL.md loses the shared rule, or one of those links loses its own sentence.
- `plan-run/SKILL.md` is about 185 lines, above the ~150 aimed for; moving more of it out would
  leave the entry without the steps every run takes.
- Tests that read the skill's text now read the file that holds each passage.
- The next sync PR rewrites `plan-pr` (and parts of the others) for the newer run flow.
