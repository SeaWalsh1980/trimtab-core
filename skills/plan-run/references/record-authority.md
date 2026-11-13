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
one — a fenced example included — is refused rather than guessed between. So is a heading that is
indented or sits behind a blockquote or list marker anywhere in the record or the plan, since
markdown renders it as a heading this reader would not see. A second line under the `## Status`
heading, a `###` included, is refused too. A CRLF file reads normally; any other carriage return
is refused.
A heading named Status or Items in a form the reader does not recognise (`# Status`, `## Items ##`, a
setext underline, an HTML heading tag) is refused too. Apart from those whole-text checks, nothing
outside the section is read. `plan complete` means the record's item is the section's last.
A plan without that section is exit 1, which is a blocker on the restart path: report it rather than
work out the next item by hand (trimtab-core ADR 0071).

The command makes the lookup repeatable; it enforces nothing, and exit 0 is not an authorisation
verdict: it does not check who merged the plan or the record (a first run with no record skips the
authorisation check entirely). The plan check in plan-run section 2 and the record's PR check below
still decide.

## Its PR: the code gate and the merger

Run `trimtab check-record-authorised --repository <owner>/<name> --plan <plan>`, as a command of its
own, with no pipe. It takes the latest record from the same lookup as `latest-record`, finds its PR,
and judges the code gate and the merger from the API, naming every failure, not just the first
(trimtab-core ADR 0072). Its admin and approver logins come only from the Trimtab instance's
configuration, never from the plan, a record, a PR or the conversation, and the admin must match the
repository's `TRIMTAB_ADMIN` variable; an instance that names no approver means no merge by the
approver counts.

| Exit | Meaning | What you do |
|---|---|---|
| 0 | the record's merge authorises it, or there is no record yet (`no record; nothing to judge`) | Use the record's `## Next` (or start item 01 part 1). |
| 1 | named problems, each on its own `FAIL` line | The record authorises nothing: stop and label its PR `plan:blocked` (below). |
| 2 | a failed read, a shape it does not expect, a stale fetch or a cut history, or an unreadable instance | Run `git fetch origin` once, wait a minute, then run it once more. A second exit 2 is a blocker. |
| 3 | the only problem is that `plan-scope`'s run on the PR's final head has not finished | Wait up to ten minutes, then run it once more. A second non-zero exit fails it, as exit 1. |

One re-run in all, whatever the first exit was.

What it holds, so a `FAIL` line reads clearly:

- **The PR:** the commit that added the record belongs to exactly one PR merged into the default
  branch, and is that PR's merge commit (a rebase merge of several commits, where the record was not
  in the last one, fails here).
- **The code gate**, whoever merged: the approver's and the admin's last review each, ignoring
  `COMMENTED` ones (a `DISMISSED` last review means no review, never an earlier one), shows at least
  one `APPROVED` on the PR's final head, and neither is `CHANGES_REQUESTED`.
- **The merger:** the admin's merge counts with the code gate. The approver's counts only when, as
  well, the head is `plan/<plan>/item-NN-partK` for this plan and this record, in this repository,
  against the default branch; every `plan-scope` job on the final head, every attempt, is a finished,
  passing job with a runner and steps from `.github/workflows/plan-scope.yml` and the `pull_request`
  event, read from job records, never a commit status; an active ruleset requiring `plan-scope` bound
  to GitHub Actions was last changed before the merge; the PR's files, read in full (`changed_files`
  over 3,000 fails), each rename's old name included, touch no admin-only path (`.github/`, the
  harness, the pin files `.claude/trimtab.json` and `instance.json`, and, where the base holds
  `bin/trimtab`, the checker's code: `bin/`, `trimtab/` and `hooks/run_module.py`); and the
  base's server-side `plan-gate` job (E22, trimtab-core ADR 0063 §7) passed. Any other merger
  authorises nothing.
- **A ruleset changed after the merge** cannot show what it required then, so it fails the test even
  for an unrelated edit, and holds the plan until the admin acts, though the merge was sound.
  Accepted: the API keeps no history of a ruleset's rules.
- **`plan-gate`:** the base ships no `plan-gate` job yet, so until it does every approver merge exits
  1 on that line alone, and the admin's removal of `plan:blocked` accepts it.

One condition stays a hand check, for an approver merge the command passed: the `plan-scope` job
runs its checker from the PR's base commit, never from the PR's own tree, so a PR cannot change what
judges it. Confirm it from the job's workflow file on the default branch. If you cannot, the
approver path is unavailable and only an admin merge counts. The file test above is a backstop, not
a substitute.

A record without the code gate, merged by any other account, or merged by the approver without every
condition authorises nothing: stop and label that PR `plan:blocked`. Never fall back to an earlier
record; it points at work already merged. The admin's removal of `plan:blocked` from that PR accepts
its merge: plan-run/references/restart.md. If you cannot read it, no session is a restart: the
stop stands.
