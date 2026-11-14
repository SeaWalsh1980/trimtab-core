# 0072. Whether the latest record's merge counts is decided by a command

## Status

Proposed (2026-11-13). Accepted when the PR that adds it merges. Changes
`skills/plan-run/references/record-authority.md` and `skills/plan-run/SKILL.md`. Item 3 of the
seven-item plan that ADR 0068 began (split the skill, sync it, then add commands for checks the
model did by hand); it builds on item 4's `latest-record` (ADR 0071).

## Context

Before a plan run takes its next work from the latest progress record, it must show that the
record's PR was merged in a way that authorises it: the code gate (an approval from the approver or
the admin on the final head, no standing request for changes), and a merger who is the admin, or
the approver with every route condition met (ADR 0043, ADR 0058). `record-authority.md` had the
model do this by hand: about eight `gh api` reads, each with its own filter, and a page of rules
for combining them. Every slip there fails open. A model that keeps an earlier approval behind a
dismissal, misses a rename's old path, reads a truncated file list as whole, or takes a commit
status for a job passes a merge that authorises nothing, and the run continues on it.

ADR 0071 showed the cost of parsing that guesses: item 4 took eight review rounds because each
heuristic missed a shape. The operator's instruction since is to fail closed: refuse with a named
problem rather than infer.

The record-authority text also held a test the brief for this item did not list: an approver merge
counts only when the base's server-side `plan-gate` job (E22, ADR 0063 section 7) passed. The base
does not ship that job yet, so the test fails every approver merge today.

## Decision

Add `trimtab check-record-authorised --repository <owner>/<name> --plan <plan> [--repo <path>]`,
in `trimtab/planrecordauth.py`.

- **Inputs are names, checked first.** The repository and plan are validated before any REST path
  or git call is built; a bad name is exit 1, since no re-run fixes it. The admin and approver
  logins come from the instance, and the admin must match the repository's `TRIMTAB_ADMIN`
  (`mergeauthority.plan_admin_problems`), as `check-plan-authorised` requires.
- **The record** is `planrecord.latest` over the fetched default branch's tip, after
  `planrecord.fetched` (the tip the server reports, a whole history) and `plan_problems` (a plan
  that exists). No record is exit 0, `no record; nothing to judge`.
- **The PR** is the one listed for the record's commit (`commits/{sha}/pulls`, one page, a full page
  an error) that is merged into the default branch. Zero or several is a named problem. Its own row
  must also be merged, target the default branch, and have the record's commit as its merge commit.
- **The code gate** reuses `mergeauthority.last_reviews` and the public aliases `review_rows`,
  `parse_reviews`, `approves`, `changes_requested` and `is_server_id` (kept at the end of that module
  for this check), so the gate at merge and this check cannot disagree. One rule is added: an approval counts only if
  its `submitted_at` is not later than the PR's `merged_at`, since a review posted after the merge
  cannot have gated it. Times have one-second resolution and the approver approves and merges back
  to back, so an approval in the merge's own second counts: that cannot be told from one a moment
  before, and refusing it would stop properly gated merges. The accepted cost is that an approval
  posted in the same second after the merge also counts, which takes a trusted account acting
  within that second. A request for changes or a dismissal still counts whenever it came, and an
  approval with no readable time is an error.
- **The merger.** The admin: pass with the code gate. The approver: every one of the following is
  judged and every failure named. The head is `plan/<plan>/<record's item-NN-partK>`; the head is in
  this repository and the PR targets the default branch (`mergeauthority.admin_reason`); the PR's
  files, a rename's `previous_filename` included, touch no admin-only path
  (`mergeauthority.admin_only`, with the checker's code held where the merge's base, the record
  commit's first parent, holds `bin/trimtab`), read in full: `changed_files` over 3,000 fails without
  a read, and a list whose length is not `changed_files` fails as partial; every `plan-scope` job
  passes (`mergeauthority.plan_scope_job_result`, which also checks the ruleset binds the check to
  GitHub Actions); an active ruleset requiring `plan-scope` bound to GitHub Actions has an
  `updated_at` strictly earlier than the PR's `merged_at`; and `plan-gate` passed. Any other
  merger, or one the instance does not name, is a named problem.
- **`plan-gate` is kept.** `judge` takes a reader for the job; the command passes none, which is
  itself a named problem on every approver merge until the base ships the job and the PR that
  ships it passes a real reader. Dropping the test would make the command pass merges the current
  text refuses.
- **Exit codes** follow `check-plan-authorised` and `check-plan-scope-job`: 0 pass, 1 named
  problems (all of them), 2 a failed read or an unexpected shape (the skill re-runs once), 3 when
  the only problem is a `plan-scope` run still going.
- **One condition stays a hand check:** that the `plan-scope` workflow runs its checker from the
  PR's base commit. Reading that from a workflow file is the kind of parse this ADR exists to avoid,
  and the file test is the backstop.

## Options considered

- **Keep the hand check, tighten the prose.** Rejected: the prose was already precise. The failure
  is in executing eight reads and their rules correctly every time, which a tested command does and
  a model does not.
- **Fold it into `latest-record`.** Rejected: that command is a lookup with no API reads beyond the
  tip check and is used on paths that judge nothing (the stop table's "record PR"). Mixing a verdict
  into it would make its exit 1 mean two things.
- **Call `mergeauthority.decide` directly.** Rejected: `decide` judges an open PR before merge, with
  check runs and the full approver route (push history, every required check, freshness). After the
  merge the question is narrower and must be read from job records, which a session's token can
  read and check runs it cannot (ADR 0058). The pieces it shares are reused instead.
- **Trust the ruleset as it is now.** Rejected: a requirement added after the merge would vouch for
  a merge it never gated. The API keeps no history of a ruleset's rules, so the only certain answer
  is a ruleset unchanged since before the merge.
- **Accept a ruleset changed after the merge when the change looks unrelated.** Rejected: deciding
  "unrelated" is the inference this ADR refuses.
- **Drop the `plan-gate` test, as the brief listed only four approver conditions.** Rejected: it
  would pass approver merges that the current record-authority text fails. If that test is wrong,
  it is removed by its own decision, not as a side effect of this one.
- **Parse the `plan-scope` workflow to confirm it runs from the base.** Rejected for now: a YAML
  reading that guesses fails open, and a strict one is its own item.

## Consequences

- The model types one command and follows a four-row exit table; the rules that failed open by hand
  are tested, one test per refusal and per exit code.
- **A false stop on any ruleset edit.** Any change to a ruleset that requires `plan-scope` after an
  approver merge (a new required check, a renamed bypass actor) fails the next session's check,
  though the merge was sound, and holds the plan until the admin removes `plan:blocked`. Accepted,
  because it fails closed. Two rulesets both requiring the check soften it: one unchanged since
  before the merge is enough.
- **Every approver merge stops until `plan-gate` ships.** That was already so under the hand check;
  the command makes it a named line rather than a step a model might skip.
- A rebase merge of several commits, where the record was added before the last one, fails the
  merge-commit test. Plan item PRs are merged with a merge commit or a squash, so this is a false
  stop the admin clears, never a pass.
- Exit 3 ignores the unshipped-`plan-gate` line: while the job is unshipped that line is on every
  approver merge and no wait clears it, so counting it would make exit 3 unreachable and turn a
  plan-scope run still going into a stop. The re-run after the wait shows the rest. Once the job
  ships, its own wait is a named problem and exits 1. All of it is tested through the real CLI;
  `planrecordauth.PLAN_GATE` is the one seam where the base's `plan-gate` reader will be set.
- Every GET is answered once per check (a memo over the `gh` runner), so the repository and the
  rules are one answer for every read of them, and cost one call.
- The check reads the server over several calls, not one snapshot. A review or ruleset changed
  between its reads is not detected; a session re-runs it at the start of every run, so the next run
  sees the change.
