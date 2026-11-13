# The latest record and its PR

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Part of plan-run/SKILL.md section 3 ("Next"). Section numbers in this file are
plan-run/SKILL.md's.

## Which record is the latest

Run `trimtab latest-record --repository <owner>/<name> --plan <plan>`, as a command of its own, with
no pipe. It reads the default branch's first-parent history (commit dates inside a branch do not
matter; when one commit added several records, the highest item, then the highest part, is latest)
and prints one line: the record's path, the SHA of the commit that added it, `item NN`, `part K`.
No records yet → it prints `item 01 part 1`. It names no plans-folder path, so the guards let it run.

| Exit | Meaning | What you do |
|---|---|---|
| 0 | the line above | Use it. |
| 1 | a named problem: a malformed record name, a malformed plan name, a plan (or its declaration) not on the default branch, a record added and since deleted, or (with `--next-on-restart`) a failed authorisation or admin check, an unreadable `## Status`, or a plan whose `## Items` section is missing, repeated, empty, out of sequence or holds a line that is not an item | A blocker: stop and report stderr. |
| 2 | a failed read, a stale fetch, a shallow clone or a grafts file; also an unreadable instance or admin variable, or a `--default-branch` that is not the default | Run `git fetch origin` (and `git fetch --unshallow origin` for a shallow clone) once, then run it again. A second exit 2 is a blocker. |

On a restart whose latest record's `## Next` is `blocked:`, add `--next-on-restart`: it prints the next work, `item NN part K` or `plan complete`, taken only from the record's file name
and `## Status` line. It also runs the plan authorisation check, so it needs the instance's admin
login; exit 1 there, or for an unparseable or mismatched `## Status`, is a blocker.

It reads the plan's items from one `## Items` section: top-level `N. ` lines numbered 1 to n in
order, with an item's wrapped text or sub-steps indented under it (a sub-step by four spaces or a
tab; a numbered line indented less is refused), and nothing else in the section: the section ends
only at the next `## ` heading or the end of the file, so a `###` heading, a rule, a fence or a
table row inside it is refused by name. Both restart cases read that section: a continuing part is
printed only once the section names its item, so an unreadable section stops the restart either way.
The record's own `## Status` is read the same way: one heading at the start of a line, and a second
one — a quoted example or an indented heading included — is refused rather than guessed between;
a second line under the heading is refused too.
Nothing outside the section is read. `plan complete` means the record's item is the section's last.
A plan without that section is exit 1, which is a blocker on the restart path: report it rather than
work out the next item by hand (trimtab-core ADR 0071).

The command makes the lookup repeatable; it enforces nothing, and exit 0 is not an authorisation
verdict: it does not check who merged the plan or the record (a first run with no record skips the
authorisation check entirely). The plan check in plan-run section 2 and the record's PR checks below
still decide.

## Its PR: the code gate and the merger

For the latest record only, find its PR: take the commit `latest-record` printed, run
`gh api repos/{owner}/{repo}/commits/<sha>/pulls`, and keep
the entries whose `merged_at` is set and whose `base.ref` is the default branch. Exactly one must
remain; none, or more than one, is a stop. Read its `merged_by.login` and `head.sha` from
`gh api repos/{owner}/{repo}/pulls/<number>`. (The progress folder is not refused by the guard, so
these reads may name it.) The approver login, like the admin's, comes only from the Trimtab instance's
configuration (`accounts.approver` from `trimtab instance --json`), never from the plan, a record, a
PR or the conversation; if the configuration names no approver login, no merge or review by the
approver counts. If it names the same login as approver and admin, the configuration is wrong: the
check fails.

First, whoever merged, the code gate (`gh api repos/{owner}/{repo}/pulls/<n>/reviews --paginate`).
Take the approver's and the admin's last review each, ignoring `COMMENTED` ones; a last review in
state `DISMISSED` means that account has no review (never fall back to an earlier one). At least one
is `APPROVED` with `commit_id` equal to the head commit, and neither is `CHANGES_REQUESTED`, on
whatever commit. So a later request for changes voids an earlier approval.

Then the merger. An admin merge with the code gate counts. An approver merge counts only when all of
these hold too, read from the API. The approver's own review may be the code gate for its own merge,
because the plan's scope, enforced by the `plan-scope` check, is what bounds the PR (trimtab-core
ADR 0043).
- the PR's `head.ref` is `plan/<plan>/item-NN-partK` for this plan, with the item and part of the
  record it carries, and its `base.ref` is the default branch. The checker passes any other head
  without judging it, so a `plan-scope` success on a non-plan PR proves nothing;
- `trimtab check-plan-scope-job --repository <owner>/<name> --head <head_sha>` exits 0, run as a
  command of its own, with no pipe, on the PR's final head commit (its `head.sha`, not the merge
  commit). It reads job records, never a commit status (anyone with write access can post a status
  under any name): every `plan-scope` job on that commit, on every attempt, must be a finished,
  passing job with a runner and steps, from `.github/workflows/plan-scope.yml` and the
  `pull_request` event. An earlier success does not outweigh a later failure. The default branch's
  ruleset must also require `plan-scope` bound to the GitHub Actions app. Exit 3 means that
  workflow's run has not completed: wait up to ten minutes for it to finish, then run it once more.
  A re-run of any job in the workflow does this. Exit 2 is a failed read: wait a minute, then run it
  once more. One re-run in all. Exit 1, or a second non-zero exit, fails this test;
- that requirement must also have held when the PR merged: read the `required_status_checks` rule
  listing `plan-scope` (`gh api repos/{owner}/{repo}/rules/branches/<default>`), then its ruleset
  (`gh api repos/{owner}/{repo}/rulesets/<ruleset_id>`); its `updated_at` must be earlier than the
  PR's `merged_at`. A ruleset changed since the merge cannot show what it required then, so the test
  fails, even for an unrelated edit; the admin's removal of `plan:blocked` is the way past it. That
  false stop has a cost: any ruleset edit after an approver merge (a new required check, a renamed
  bypass actor) stops the run at the next session and holds the plan until the admin acts, even
  though the merge was sound. Accepted, because the API keeps no history of a ruleset's rules;
- for a PR from a plan run, the job the pinned base names for the enforcement command on the
  server (E22: `plan-gate`, trimtab-core ADR 0063 §7) passed on the PR's final head, read from job
  records the same way as `plan-scope`: every attempt finished and passed, from the pinned workflow
  path and the `pull_request` event. A pinned base that names no such job, or a job that did not
  pass or cannot be read, fails this test, so only an admin merge counts then. Until the base ships
  the `plan-gate` template and the project adopts it, this test fails for every approver merge:
  each one stops the next session, and the admin's removal of `plan:blocked` accepts it;
- the PR changes neither `.github/` nor the code the `plan-scope` check runs
  (`gh api repos/{owner}/{repo}/pulls/<n>/files --paginate`): no file's `filename`, nor for a rename
  its `previous_filename`, is under `.github/`, is either root file that can pin the checker,
  `.claude/trimtab.json` (the Trimtab lock) or `instance.json` (an instance's pin), both checked in
  every repository, or, in trimtab-core itself, is under the checker's `trimtab/` package or `bin/`,
  or is `hooks/run_module.py`, which `bin/trimtab` runs to load the package. A PR that edits, moves
  or removes a workflow or the script it calls controls the checks it runs. That endpoint lists at
  most 3,000 files; when the PR's `changed_files` is larger, this test fails;
- the `plan-scope` job runs its checker from the PR's base commit, never from the PR's own tree, so
  a PR cannot change what judges it. Confirm it from the job's workflow file on the default branch.
  If you cannot, the approver path is unavailable and only an admin merge counts. The file list
  above is a backstop, not a substitute.

A record without the code gate, merged by any other account, or merged by the approver without every
condition authorises nothing: stop and label that PR `plan:blocked`. Never fall back to an earlier
record; it points at work already merged. The admin's removal of `plan:blocked` from that PR accepts
its merge: plan-run/references/restart.md. If you cannot read it, no session is a restart: the
stop stands.
