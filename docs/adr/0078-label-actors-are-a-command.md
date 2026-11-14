# 0078. Who set or cleared a plan label is decided by a command

## Status

Proposed (2026-11-14). Accepted when the PR that adds it merges. Changes
`skills/plan-run/SKILL.md` and `skills/plan-run/references/restart.md` and `in-flight.md`. Item 5 of
the seven-item plan that ADR 0068 began (split the skill, sync it, then add commands for checks the
model did by hand); it follows `latest-record` (ADR 0071) and `check-record-authorised` (ADR 0072).

## Context

Two plan-run decisions turn on who acted on a label, not on what the label says now:

- a session started by the `plan:continue` label must stop unless the agent, approver or admin
  account added it, because anyone with triage rights can add a label and the label starts a run;
- a restart is valid only if the admin removed `plan:blocked` from the stopped PR after the stop,
  because the same people can remove it too.

`plan-run` had the model read `gh api .../issues/<n>/events` and work both out by hand. The
slips all fail open: a read cut off at one page, the first matching event taken instead of the
latest, an older removal from an earlier stop counted for a later stop, a removal by the wrong
account accepted because the label is gone, or the stopped PR picked from the open PRs only. The
operator's instruction since ADR 0071 and ADR 0072 is to fail closed: refuse with a named problem,
never infer.

Nothing in the base already decides this. `planacts` and `planguard` decide which label writes a
hook allows, `mergeauthority` reads blocking labels on a PR now, and `planrecordauth` judges a
merge; none reads who set or cleared a label.

## Decision

Add `trimtab check-label-actor`, in `trimtab/planlabels.py`, with two modes. Exit codes follow
`check-plan-authorised`: 0 pass, 1 named problems (all of them), 2 a failed read or an unexpected
shape. Names are validated before any REST call; the admin must match the repository's variable
(`mergeauthority.plan_admin_problems`), and the logins come from the instance, never from a PR.

1. **`--repository <o/n> --label plan:continue --pr <N>`.** The PR (and that it is a PR) and its
   labels are read from `issues/<N>`, and every page of `issues/<N>/events`. The label must be on
   the PR now; its most recent `labeled` event must exist, must be the label's last event, and its
   actor must be the instance's agent, approver or admin, compared without case. Only
   `plan:continue` is accepted as `--label`.
2. **`--repository <o/n> --restart --plan <plan>`.** Candidates are every PR, open or closed, whose
   head ref starts `plan/<plan>/item-`, and the one PR that added the plan file (found from the
   adding commit in first-parent order, as `check-plan-authorised` does). The newest `labeled`
   event for `plan:blocked` among them marks the stopped PR. It passes only if exactly one
   `unlabeled` event follows that event, its actor is the admin, and the label is absent now. No
   such event anywhere is exit 1, `nothing to restart`. The stopped PR's number is printed alone.

Fail-closed rules shared by both modes:

- Every list is read in full with a bound; more rows than the bound is exit 2, never a short list.
  The PR list is read oldest first, so a PR opened during the read lands on a later page.
- Events must have rising ids and non-falling times across the whole list, and each label event
  must name its label and carry an `actor` key (a null actor is a deleted account and a named
  problem, a missing key is exit 2). A list not in order is not read.
- Two stops on different PRs in the same second cannot be ordered: a named problem, never a guess.
- The newest stop is the one judged: an older stop that was never cleared does not block a newer,
  cleared one, and an older admin removal does not clear a newer stop.
- A label whose last event is a removal but which is present now, and a stop followed by more than
  one removal, are named problems: the events and the label disagree.

## Options considered

- **Keep the by-hand reads and tighten the skill text.** Rejected: that is what failed open. Text
  cannot enforce pagination, ordering or case-insensitive comparison, and the review rounds on
  ADR 0071 show where prose guards end.
- **Read the repository's timeline (`issues/<n>/timeline`) instead of the issue events.**
  Rejected: it holds more event kinds, and the field that names the actor differs by kind, so
  there would be more shapes to refuse for no more facts. The issue events list carries the label,
  the actor and the time.
- **Find the stopped PR from the open PRs, or from PRs labelled `plan:blocked` now.** Rejected: a
  restart is exactly the case where the label has been removed, and a stopped PR may be closed.
  A list filtered by label now would never show it.
- **Search the issues (`search/issues`) for the plan's PRs.** Rejected: the search API is not a
  repository-scoped path, which `GhApi` refuses for cloud sessions, and its index lags.
- **Order stops across PRs by event id.** Rejected as the rule, kept as a within-PR check: event
  ids are one sequence, but nothing documents that they order across issues, and a guess here lets
  an older stop pass for the newest. Time ties are named instead.
- **Read the repository-wide events list (`issues/events`) once instead of one events read per
  candidate PR.** Rejected: the doctrine's rule against a request per row points at it, but the list
  is newest first (so the per-PR order check does not carry over), and it grows with every event on
  every issue and PR in the repository, not with the plan. Measured on this repository: 1,455 events
  over 15 pages for about 180 PRs. A per-candidate read grows with the plan and is bounded by
  `CANDIDATES_LIMIT` (100 reads, each usually one page), so a busy repository would make the batched
  read exit 2 on its bound for good. The per-candidate loop is therefore kept as a bounded fan-out,
  and the rule's wording is raised upstream as feedback rather than treated as met.
- **Judge every PR that ever carried `plan:blocked`.** Rejected for this command: it asks one
  question, whether the plan's most recent stop was validly cleared. An older stop left uncleared is
  the in-flight check's concern (`plan-run/references/in-flight.md`).

## Consequences

- The model no longer reads label events for the two decisions this command judges: who added
  `plan:continue`, and whether the plan's newest stop was cleared by the admin. `plan-run` runs the
  command and acts on its exit code; the skill text says what each code means. The open-PR row of
  `in-flight.md` still has the model read label events by hand, for an open PR with an older stop
  that is not the plan's newest. That gap is accepted here and is a follow-up: it needs a command
  that judges a named PR, not the newest stop.
- A restart reads the PR list and every candidate's events twice and exits 2 if they differ, so a
  stop labelled during the read cannot be missed. This doubles the reads.
- A repository with more than 5,000 PRs, or a plan with more than 100 candidate PRs, makes
  `--restart` exit 2 until the bounds are raised. The cost of a restart check is the PR list pages
  plus one events read per candidate.
- A restart check needs a fetched checkout of the default branch, as the other plan checks do, and a
  stale fetch is exit 2.
- Two stops in the same second block a restart until one is cleared by hand. This is accepted: it
  needs two sessions stopping in the same second.
- The check reads the server at one moment and re-reading is not atomic: a label changed between the
  reads is at worst a named problem. Nothing here authorises a run: the label and a restart still
  only start a session, which then runs the authorisation checks.
