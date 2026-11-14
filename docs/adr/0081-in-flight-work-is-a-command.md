# 0081. What a plan has in flight is decided by a command

## Status

Proposed (2026-11-14). Accepted when the PR that adds it merges. Changes
`skills/plan-run/SKILL.md` and `skills/plan-run/references/in-flight.md`. Item 6, the last core
item, of the seven-item plan that ADR 0068 began (split the skill, sync it, then add commands for
checks the model did by hand); it follows `latest-record` (ADR 0071), `check-record-authorised`
(ADR 0072) and `check-label-actor` (ADR 0080). Item 7, the pin move and adoption, comes after it.

## Context

Before a run takes the next work from the latest record, it must know whether the plan already has
work in flight: an open item PR, or a pushed `plan/<plan>/item-*` branch that never had a PR. If it
does, that work is resumed before anything else, so a wrong answer either builds on work the run
may not trust (a fork's, a stranger's, a stopped part's) or starts a second part beside a live one.

`plan-run/references/in-flight.md` had the model do this by hand: list the PRs, read each one's
head, author and labels, read each one's label events for a `plan:blocked` stop and a later admin
removal, ask the server whether a branch ever had a PR (`pulls?state=all&head=<owner>:<branch>`,
one request per branch), and judge a branch with no PR against the plan's newest stop. The slips
fail open in the way ADR 0080 lists: a list cut off at one page, a fork's PR whose branch name
matches, an author compared with case, the first stop taken instead of the newest, a label read as
present-now instead of from its events. The operator's standing instruction since ADR 0071 is to
fail closed: refuse with a named problem, never infer.

ADR 0080 left this gap on purpose: "the open-PR row of `in-flight.md` still has the model read
label events by hand ... it needs a command that judges a named PR, not the newest stop." This is
that command. Nothing else in the base decides it: `planlabels` judged only the plan's newest stop
(`--restart`), `planacts` and `planguard` decide which writes a hook allows, `planrecord` finds
records, and `planauth` and `planrecordauth` judge a merge.

## Decision

Add `trimtab check-inflight --repository <o/n> --plan <plan> [--repo DIR]`, in
`trimtab/planinflight.py`, with the label judgement added to `trimtab/planlabels.py`
(`check_stops`). Exit codes follow `check-plan-authorised`: 0 a verdict, 1 named problems (all of
them), 2 a failed read, a list cut off at its bound or an unexpected shape. Names are validated
before any REST call; the admin must match the repository's variable
(`mergeauthority.plan_admin_problems`); the agent and admin logins come from the instance, never
from a PR.

Two list reads, each bounded, and no request per row:

1. every PR of the repository, any state, oldest first (`pulls?state=all`, at most 5,000 rows);
2. the plan's branches (`git/matching-refs/heads/plan/<plan>/`, at most 500 rows).

**An open PR** whose head ref starts `plan/<plan>/item-` is resumable only if all hold, and each
failure is named separately:

- the head ref has exactly the shape `plan/<plan>/item-NN-partK` (two digits for the item, a part
  number from 1 with no leading zero);
- its head repository is this repository, compared without case. A fork, or a head repository that
  was deleted, is refused naming it;
- its author is the agent or the admin account, compared without case. A deleted account is refused;
  an instance that names no agent leaves only the admin;
- any `plan:blocked` stop was cleared. The PR's `plan:blocked` events since the plan merged come from
  **one** read of the repository-wide issue events (the window ADR 0080 built), for all open PRs at
  once. The newest `labeled` event is its stop; exactly one `unlabeled` event must follow, by the
  admin, and the label must be gone from the PR's own row. A label with no event to explain it, or a
  removal with no stop before it, is a named problem (the history cannot be read). This is the same
  judgement `--restart` makes, moved into one function (`_removal_problems`) both call, now for a
  named PR instead of only the newest stop.

**A branch with no PR** is a branch `plan/<plan>/item-*` that no PR of this repository has as its
head in any state. A closed PR from a fork that happens to share the name does not count as a PR of
the branch. It is resumable only if its shape is right (otherwise refused, named) and the plan's
newest stop anywhere was cleared or there is none: the `--restart` judgement. "There is none" is
the typed flag `Verdict.no_stop`, which the judgement sets; the command never matches message text
(a prefix match was built first and rejected in review: it couples a verdict to wording).
`planlabels.NOTHING_TO_RESTART` stays as the human-readable opening of that message. Branches under
`plan/<plan>/` that do not start `item-` are not in flight and are not reported, as the skill defines
in-flight work.

**Verdict.** Exactly one resumable thing prints `resume pr <n> <branch> <sha>` or `resume branch
<branch> <sha>` (the head commit the server reported) and exits 0. Nothing in flight prints `none in
flight` and exits 0; the line cannot be mistaken for a resume. More than one resumable thing, or any
refusal, exits 1 and prints nothing on stdout: every problem is listed, and a refused PR next to one
good PR is still a refusal, never "resume the good one".

The plan is always checked to be a plan on the fetched default branch, even when nothing is in
flight, so a mistyped plan name is a named problem and never `none in flight`.

Two scopes of stop, kept as the skill has them: an open PR is judged against **its own**
`plan:blocked` events only, and a branch with no PR (which has no events of its own) against the
plan's newest stop anywhere. So an uncleared stop on a closed PR or on the plan's own PR refuses a
branch but not an unrelated open PR with no stop of its own. That is the skill's two rows, not a new
rule; narrowing or widening it is a change to the run-flow, not to this command. A branch that a
closed PR once had is not in flight and is not reported: the skill's definition is "never had a PR".
The branch list is read before the PR list, so a PR opened between the reads is judged as a PR.

## Differences from the skill text it replaces

- **Branches with a PR in any state are read from one list, not one request per branch.** The skill
  asked `pulls?state=all&head=<owner>:<branch>` per branch. This reads `pulls?state=all` once and
  indexes the heads of this repository's PRs, so a closed or merged PR still hides its branch, as
  the skill intended. The task brief for this item said "no open PR has it as head"; that would
  report a merged item PR's leftover branch as in flight and resume it, so the any-state reading is
  kept and the list read is the one `--restart` already reads (the same 5,000-row bound).
- **A fork's closed PR does not hide a branch of the same name.** The skill's `head=<owner>:<branch>`
  filter named the owner; here the head repository is compared.
- **Part numbers start at 1 and take no leading zero.** The skill said `item-NN-partK` without
  fixing K. A `part0` or `part01` branch is refused as the wrong shape rather than resumed, since
  nothing writes one.

## Options considered

- **Keep the by-hand reads and tighten the text.** Rejected: that is what failed open. Pagination,
  case and "newest stop" are exactly what prose does not enforce.
- **Read each open PR's own events (`issues/<n>/events`), one request per PR.** Rejected: a request
  per row in a loop, which the doctrine's rule against that pattern (a PROHIBIT) forbids; ADR 0080
  had to replace the same shape. The repository-wide window answers every open PR at once.
- **List only open PRs and treat any `plan/<plan>/item-*` branch with no open PR as in flight.**
  Rejected: a merged or closed item PR leaves its branch behind unless it is deleted, and that
  branch would be resumed. Only a list of every PR says the branch already had one.
- **Ask `pulls?head=<owner>:<branch>` for each branch.** Rejected: a request per row, and a count of
  branches that grows with the plan.
- **Reuse `check-label-actor --restart` for every PR.** Rejected: it names only the plan's newest
  stop, so an older, uncleared stop on an open PR would pass while a newer one elsewhere was cleared.
- **Prove the pushes (chain of update events, fast-forwards, scope diff, plan-run environment).**
  Out of scope: E13 in ADR 0063 stays with the guards and the by-hand section of `in-flight.md`.
  Exit 0 says only that nothing the command reads refuses the work; the reference says so.
- **Point `planrecordauth._merged_pr` at `planauth.merged_prs_of`.** Out of scope, noted: a separate
  cleanup that does not change a verdict.
- **A separate exit code for "none in flight".** Rejected: the exit codes follow
  `check-plan-authorised`, and `none in flight` on stdout is exact and cannot be a resume line.

## Consequences

- The model no longer reads PRs, authors, labels or label events to decide what is in flight.
  `plan-run` runs the command and acts on its exit code; `in-flight.md` says what each code means,
  and a test pins the wording.
- The by-hand label reads are gone from `in-flight.md`. The pushes section stays by hand, with
  E13's guards behind it.
- Cost, from the tests' fake server (a real server returns the same shapes, checked on one run):
  with only open PRs in flight, a check makes one PR list, one branch list, one events window
  (up to 50 pages of 100, the cap ADR 0080 set) and the plan-context reads (default branch, its tip,
  the plan-adding commit's PRs, that PR). A branch with no PR adds `--restart`'s judgement: two more
  PR lists, two more windows and one `issues/<n>` read, so the worst case is about 150 event pages
  and 3 PR lists. This duplicates the PR list and window reads when both an open PR and a branch
  exist; sharing one window between the two judgements is possible, and not done because it would tie
  the open-PR judgement to `--restart`'s candidate set and its two-snapshot rule for no case that has
  appeared. A plan with nothing in flight pays for the two lists and the plan context only.
- The PR list is read in full every time, as `--restart` does, so a repository with over 5,000 PRs
  makes the command exit 2 until the bound is raised, and the cost grows with the repository's PR
  count. A plan merged long ago in a busy repository reaches the events cap; exit 2, never a shorter
  window.
- The plan-wide "no stop" for a branch with no PR is `--restart`'s: a stop labelled before the plan
  merged is outside the window, so a removal after the merge with no stop before it counts as an
  older, dropped stop (ADR 0080 has a test that pins this). Open PRs are stricter: a removal with no
  stop before it, or a label with no event, is refused. The looser rule applies to closed PRs and the
  plan's own PR only, and a branch with no PR also needs the exact shape.
- A PR opened while the list is read lands on a later page (oldest first); a PR that appears twice
  because pages shifted is exit 2, run it again.
- A fork head, a foreign author and a standing stop are exit 1 and block a run from starting new
  work for that part; only a valid restart (`check-label-actor --restart`) resumes it.
- No account name or instance ID appears in this base: logins come from the instance.
- Measured once against a real repository on 2026-11-14 (a plan name the repository does not have,
  since it has no authorised plan): the lists read with a 200 (`matching-refs` with a trailing slash
  returns `[]` when nothing matches), and the command exits 1 with `is not a plan` for both files.
  That shows the read paths and the always-checked plan, not a live item PR; the cases with PRs and
  branches are the tests'.
