# 0030. Project drafts are checked by the tool, by merge and close time

## Status

Accepted on merge (2026-10-17). Amends ADR 0006
(`docs/adr/0006-the-loops-state-lives-on-github.md`): its sentence "The
session applies the same rule to a project's draft pull requests, matching by
title". The rest of ADR 0006 stands, including the rule for upstream issues.

## Context

ADR 0006 deduplicates proposals against GitHub. The tool skips an upstream
issue when one titled `[<ID>]` is open or was closed within the window, and
the session applied "the same rule" to project draft PRs: it listed the PRs by
hand and skipped an ID whose `[<ID>]` PR was open, or was closed or merged
within the window, judging "within the window" by `updated_at`.

On 2026-10-17 a consumer's retro listed one of its project rule IDs at
threshold although a fix for it had merged two days before. The session
skipped the ID, but for the wrong reason: the same run's ingest had labelled
the fix PR, which bumped its `updated_at` into the window. Reading the rule as written:

- any touch to a PR (a label, a comment, a bot) brought it back into the
  window, merged or declined;
- a merged fix held the ID back for the whole window, so evidence reported
  after the fix could not propose it again;
- the check lived in prose, so each session re-implemented the paging, the
  bound and the matching, and step 3's conformance candidates had no
  machine check at all.

## Decision

`trimtab propose --dry-run --check-open` checks project drafts itself, from
one bounded REST listing of the project's PRs, and prints a mark beside each:

- **Open.** `SKIPPED` while an `[<ID>]` PR is open.
- **Merged fix.** Only evidence merged after the latest merged `[<ID>]` fix
  counts: the fix saw everything before it. Below the threshold the draft is
  `RESOLVED` (saying how many reports came since); at it, `REOPENED`, naming
  the fix and the reports since. Order is by merge time, never PR number. A fix
  that is itself evidence is left out of the count.
- **Decline.** `SKIPPED` while an `[<ID>]` PR closed unmerged within the
  window, judged by when it closed, unless it closed before the latest merged
  fix, which supersedes it.
- **Unreadable.** `NOT CHECKED` when the listing could not be read; the run
  ends 2, and the issue plan is still printed.
- **Conformance candidates.** `--check-ids ID=<latest report day>` gives an ID
  with no ledger draft the same marks, `RESOLVED` by a fix merged on or after
  that day.

The session opens a draft only for an unmarked or `REOPENED` ID.

## Options considered

- **Keep ADR 0006's rule, judged by `updated_at`.** Rejected: that run
  showed the ingest's own label deciding the outcome, and any later
  comment on an old decline blocks fresh evidence for the whole window.
- **Order fix and evidence by PR number.** Tried first in this change and
  rejected in review: a PR is numbered when it opens, so a fix opened after the
  evidence but merged before some of it would be called RESOLVED without
  having seen that evidence, and one opened early but merged last would be
  proposed again.
- **Count all evidence in the window after a fix.** Rejected: one report after
  a fixed rule, plus the reports the fix already addressed, re-proposed it.
- **Leave the check to the session, in prose.** Rejected: each session paged
  and matched by hand, the rule drifted from the code that applies it to
  issues, and conformance candidates were never checked. The tool is
  deterministic and tested; the session reads its marks.
- **Hold any ID with a merged `[<ID>]` PR in the window** (ADR 0006's effect).
  Rejected: it suppresses real evidence that arrives after an incomplete fix.

## Consequences

- The marks are reproducible and covered by unit and CLI tests; the retro
  command describes them rather than re-implementing them.
- A fix merged before the window opens is not listed. The draft is still
  proposed, but marked plainly rather than `REOPENED`, without naming the old
  fix. Accepted: seeing it would mean scanning past the window's bound.
- The closed-PR scan continues across calls within one run instead of
  restarting, so each page is fetched once; a PR updated between two page
  reads seconds apart could be missed. Accepted as the same page-shift risk a
  single scan already carried.
- Matching is by the ID a title opens with, exactly as the retro writes it
  (`[<ID>] <what changes>`); a hand-made title naming several IDs, or another
  case, is not matched.
- Step 5's structure-PR check still lists PRs by hand; moving it into the tool
  is left to a later change.
