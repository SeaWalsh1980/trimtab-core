# 0006. The loop's state lives on GitHub

## Status

Accepted.

## Context

The loops of ADR 0005 run weekly as cloud routines: a retrospective in each
consuming project, and an upstream retrospective on the instance. A routine
starts from a fresh sandbox every run, with a clean clone, no persistent home
directory, and pushes only to branches it names. Three pieces of loop state
must still survive from one run to the next:

1. **The ingest ledger.** A proposal needs feedback on one ID from 2 or more
   pull requests. Those usually arrive in different weeks, so the tally has
   to see past runs. The first implementation wrote the ledger to a JSON
   file at a path the caller passed in.
2. **The structure baseline.** The structure check proposes a change when the
   always-loaded instructions grow by more than 10% since an earlier report.
   The first implementation read that report from a file.
3. **What has already been proposed.** A weekly run must not file the same
   issue again, and must not re-file one the operator declined. A scheduled
   job has to be idempotent.

The constraints: add no infrastructure unless it is needed; put a bound on
any result set that grows with the data; and keep the merged PR body as the
durable carrier of the evidence.

## Decision

GitHub already holds everything needed, so the loop keeps no state of its
own.

- **The ledger is rebuilt each run from the pull requests already
  labelled** (`--ledger-from-labels`). The label is the watermark and the
  merged PR body is the record. The rebuild uses the same parser as ingest
  and is keyed by PR number, so it is idempotent. The read has two bounds.
  The first is an 8-week window: evidence older than that has decayed and no
  longer counts, and the same window applies to every tally, file ledgers
  included. The second is `--history-limit`, which defaults to 500, reads
  newest first, and reports when it truncates. Recording before labelling
  still holds: a run killed between reading and labelling reads the PR again
  next time and counts it once. `--ledger <file>` remains for local and
  scratch runs.
- **The structure baseline is the committed tree at a ref**
  (`lint structure --against <ref>`), exported with `git archive` into a
  temporary directory. The retrospective passes the default branch as it
  stood one cadence ago.
- **Proposals are deduplicated against GitHub.** The tool opens only
  upstream issues, behind the same dry-run and confirmation gate as ingest.
  It skips an ID when an issue titled `[<ID>]` is open, **or was closed
  within the window**. The session applies the same rule to a project's draft
  pull requests, matching by title. Each issue names the project that
  reported it, so the upstream loop can count distinct consumers per ID.

## Options considered

- **Commit the ledger file to a dedicated branch.** Rejected. Every run
  would push, the branch would need protecting, and concurrent runs would
  have to merge. A routine pushes only to branches its session names, so a
  long-lived state branch would need its own setup. It would also duplicate
  what the PR bodies already hold.
- **Keep the ledger in an issue body or comment.** Rejected. An issue body is
  capped at 65,536 characters and the ledger grows with every PR. Anyone with
  write access can edit it, and one bad edit loses the ledger. That is fine
  for a handful of operator decisions and wrong for a record per PR.
- **Have the retrospective's own pull request commit the ledger to the
  default branch.** Rejected. It lands only when a person merges it, so the
  next run finds it missing and counts the same PRs again.
- **Cloud storage, such as a bucket or a database.** Rejected. It needs new
  infrastructure and credentials for the routine to hold, for state GitHub
  already has.
- **Rebuild from every labelled PR, with no window.** Rejected. The read
  would be unbounded, and without decay, evidence about a rule that has since
  been rewritten would keep counting.
- **Deduplicate against open issues only.** Rejected. An issue the operator
  closed as declined would be filed again the next week from the same
  evidence.
- **Keep report files for the structure check.** Rejected. It has the same
  disk problem as the ledger, whereas a ref is exact and costs one
  `git archive`.

## Consequences

- A routine needs no disk, no state branch and no extra credentials. Its
  only GitHub writes are the label, the issues and the draft pull requests.
- Each run reads the window's labelled PR bodies in one paginated
  `gh pr list` call. Measured on a busy project: 356 PRs merged in 8 weeks
  (about 45 a week), and reading 300 bodies took 2.4 s. The default limit
  was therefore set to 500, not the 300 first written. A busier project can
  raise `--history-limit`, and a run that hits the limit says so.
- Accepted: a PR body edited after merge is read as edited, so evidence can
  change after the fact. This is the same self-reporting exposure as the PR
  block itself (ADR 0005), and the label still prevents double counting.
- Accepted: a declined issue can be filed again once its evidence has aged
  out of the window and new reports arrive. That is decay working as
  intended, not a leak.
- The labels `trimtab-ingested` and `harness-feedback` must exist before the
  first apply. The retrospective stops and reports when they are missing,
  and never creates them itself.
