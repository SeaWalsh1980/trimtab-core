# 0069. The plan skills follow the flow where a run owns its PR to merge

## Status

Proposed (2026-11-12). Accepted when the PR that adds it merges. Changes the behaviour of
`skills/plan-run/`, `skills/plan-item/` and `skills/plan-pr/`, which ADR 0068 split out of
`wrap-merged` with no behaviour change. Lands ADR 0063's planned skill changes (sizing parts to one
session, §1b; the watch step, §5a, and watch-only, §1a), which ADR 0068 said would land in these
skills. Earlier records stand as written; where 0060 and 0062 describe rounds "before ready" and
two CI fix rounds, this record and the doctrine record below replace that for the skills.

## Context

The instance's doctrine changed the plan-run flow in its record `0027-plan-runs-own-their-pr-to-merge.md`
("the doctrine record" here, so that it is not read as this base's ADR of that number), merged
into the instance's rule for running an authorised plan. The skills still taught the old flow:

- the draft opened when the work started, and one to three in-session rounds gated ready;
- CI review ran after ready, usually after the session had ended, and a separate session fixed its
  findings, at most two CI fix rounds per PR;
- check 4 of the authorisation check was read by the session, or attested by the operator.

The new flow, in order: the run works on its `plan/…` branch with pushed checkpoints and no PR;
up to five in-session rounds gate the draft; CI review runs on the draft and the run waits for it
(bounded); every CI thread gets a tagged reply; each CI fix gets one in-session round and a
re-request through a named mechanism; ready only after CI review passes; then the run watches its
PR to merge (bounded), owning it until it merges or a stop applies. At most three CI fix rounds per
item part, across every PR and branch. Check 4 is shown only by the enforcement command (E20,
`trimtab check-enforcement`, ADR 0063 §2), never attested.

The mechanisms behind that flow (E1–E23) are guard and job work in other PRs (ADR 0063). The
skills only describe what a run does and stops on; where they describe a check the model applies by
hand (the record's merger, the latest record, the label actor, in-flight work), that prose stays
until tested `trimtab` commands replace it in later PRs.

Review of ADR 0068's PR left carry-overs: when `stops.md` is unreadable, the `plan:blocked` label
and the record's `## Next` can disagree; the test's pinned root-line wording was undocumented; and
some deferred LOWs (moved sentences still saying "below", a dangling lead-in colon, a wrong bullet
count, test gaps). A review of the original skill found stacked exceptions where a decision table
would read better, an implicit `clear_session` fallback, and an unstated cost of the ruleset
`updated_at` test.

No measurement drove this record. The round and time numbers are the doctrine record's: five
rounds and three CI fix rounds are the operator's call, and the 60-minute and 24-hour bounds are
unmeasured defaults a project may override.

## Decision

- **plan-item** owns the part before the draft: sizing it to one session (ADR 0063 §1b), the
  branch with pushed checkpoints and no PR, scope and the fix limits, the record and register
  (an entry written before the PR names the branch; each round's outcome goes in the record), the
  one-to-five in-session rounds, opening the draft and labelling it. New references:
  `scope.md` (in-scope by the diff from the merge-base; a failing check is in scope when it passes
  on the merge-base; the fix limits) and `rounds.md` (a round; the pre-draft table; the one round
  after each CI fix). The session that opens the draft owns the PR.
- **plan-pr** is rewritten to take the draft to merge: ownership (and what a later session without
  the round ledger may do), the CI review wait, tagged replies, CI fix rounds and their count, the
  named re-request, ready, the watch, and the merge gate. New references: `ci-review.md` (which
  review counts, verdicts, the outcome-tag table) and `watch.md` (the watch's events, conflicts
  with the default branch as a CI fix round, watch-only).
- **plan-run**: check 4 runs the enforcement command and fails on anything else, including a base
  that lacks it; in-flight work includes a pushed `plan/…` branch that never had a PR, resumed only
  when it holds only the run's own in-scope pushes (new reference `in-flight.md`); a stop before the
  draft labels the merged PR whose record supplied the next work (the plan's own PR for item 01) and
  removes `plan:continue` from it; restarts gain the new-part rules (branch deletion, part K+1, per
  part counts); the approver merge also needs the E22 job; the always-refused list gains the
  enforcement check's sources.
- **The shared unreadable-link rule** now names the same label target as the stop protocol, and
  says the label is authoritative when the record's `## Next` could not be updated. `stops.md`
  says so too, as a first-row-applies table by where the part is, with the stops that forbid a
  write. Every stop but an unconfirmed merge leaves a label; the old exemption for a restart that
  had not reached a branch is gone, since a restart has a record PR to label.
- **Fail closed where the text left a gap:** ready needs a `pass` verdict on the exact head, not
  only the absence of a CRITICAL or HIGH one; any `fail-closed` verdict is re-requested once; a
  review pass that fails twice on one head is a stop; a failed read anywhere in the in-flight table
  is a stop; an infrastructure failure that fails its re-run is an ordinary failing check.
- **No write without authority:** a failed authorisation check or gate, in any session, and
  in-flight work the run may not build on, forbid every file write; the stop only labels.
  `stops.md` keeps those apart from the stops whose one write is the stop record itself (a
  secret-scan failure, a stray Bash write). Only a `fail-closed` verdict that posted no thread is
  re-requested without a round, as the doctrine's "before reaching any finding" reads; one that
  posted threads is handled like a `fail`. A
  ledger the session cannot read counts as none. A label write that fails twice is reported as
  "stopped but unlabelled". An open blocked PR is judged by its label events, not the label's
  presence. A `fail` verdict that leaves nothing to fix, an in-scope finding the fix limits
  forbid fixing, an unreadable merge-base result, and a request for changes by the approver or
  admin are stops. The self-asserted tags (wrong, intentional, moot) need their citation.
- **Form:** steps are decision tables or if/then lists; each SKILL.md stays under 180 lines, with
  detail in references. The "not merged" stop says it reports only; the `clear_session` fallback
  is a table row; the ruleset `updated_at` test names its cost.
- **Tests** (`tests/trimtab/test_skills.py`): the shared rule must sit above an entry's first link;
  every link, in any plan skill file, to one of the four deciding files must give its meaning after
  "If you cannot read it, ", in its own paragraph, list item or table row; a link may not climb
  with `..`; a `#anchor` must name a heading (frontmatter and fenced code skipped by CommonMark's
  fence rules, repeats numbered as github-slugger does), and an anchor on an extensionless link is
  refused; each SKILL.md is held to 180 lines; each root-line wording is accepted only in its own file type,
  and a comment says why the wording is pinned. `test_mergeauthority`'s skill-wording checks follow
  the text to its new places.
- ADRs 0060, 0062 and 0068 gain an "Amended by" line pointing here.

## Options considered

| Option | Why it lost |
|---|---|
| Keep plan-pr as "review and ready", and add a fourth skill for the watch | The watch, CI review and ready are one ownership loop with one set of round counts; a fourth description competes for the same triggers (ADR 0068's reason against an alias) |
| Put the in-session rounds in plan-pr, as before | The rounds now gate the draft, before any PR exists; plan-pr would load for work that has no PR. The round after a CI fix links the same `rounds.md`, so the text is not duplicated |
| Let the skill read `CODEOWNERS` or accept an attestation for check 4 | The session would judge its own gate; the doctrine record makes the command's exit the only fact, and the guards refuse every run write while it fails (E21) |
| Build the record-authority, latest-record, label-actor and in-flight checks as commands here | Mixes behaviour sync with new code, so neither can be reviewed alone; the agreed sequence gives each its own tested PR, and the prose stays until then |
| Restate E1–E23 in the skills | The rows are guard and job work, owned by ADR 0063; a skill that restated them would drift, and only a guard counts as enforcement. The skills name a row only where a run meets its refusal |
| Leave the shared rule's label target as "the merged PR that started the session" | Disagrees with the new stop protocol, which labels the PR whose record supplied the work; the two would label different PRs for the same stop |
| Keep stacked exception prose | The original review found a reader had to hold every exception to apply any one; tables put each case on one row |
| Make the watch bound running out a stop, with a label (raised in review) | The doctrine says reaching the bound unmerged is reported, not a stop; a skill may not loosen or redefine the flow, and a ready PR that is only slow needs no admin act to continue |

## Consequences

- A run now holds its session from the first push to merge: longer sessions, more billed CI
  reviews, bounded waits. Parts must be sized to fit, or end early at a clean point.
- Until ADR 0063's guards and the consuming project's rows land, check 4 fails and no plan runs,
  by design. Several skill steps cite refusals (E5–E23) that do not exist yet; a run never reaches
  them while check 4 fails.
- **Open question, for the instance:** E23 refuses a draft unless the ledger holds a complete round
  on the exact head, but the doctrine also writes every round's outcome to the record, so the head
  moves after the last round. plan-item asks for the last reviewed head plus at most one
  record-and-register commit, mirroring the ready exception, and says that until the row changes,
  E23 refuses it, which is a stop. This base may only tighten a row, so the fix needs a new instance
  decision record that edits the rule's E-row sentence: narrow E23 to allow a record-only commit of
  E7's shape, or drop "before the draft" for the last round's line. Until then, every draft is
  refused once E23 lands.
- The ruleset `updated_at` test still stops a run after any ruleset edit following an approver
  merge; the skill now says so. A `check-record-authorised` command may narrow it later.
- Consumers adopt the change only when their pin moves to a base that carries it.

## Measurements

None. The line counts after the change: `plan-run/SKILL.md` 174, `plan-item/SKILL.md` 109,
`plan-pr/SKILL.md` 122.
