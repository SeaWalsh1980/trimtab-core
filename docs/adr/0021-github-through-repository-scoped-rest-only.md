# 0021. GitHub through repository-scoped REST only

## Status

Accepted (2026-10-16), by the operator: the trial passed on a retro run and a
conformance run (see "Trial result" below). The notes below are kept as they
were written.

Proposed (2026-10-12). Narrows ADR 0019. Becomes accepted once a guarded cloud
routine runs `ingest` and its command end to end on this base (see "Trial,
still open" below).

## Context

ADR 0019 moved every GitHub call to `gh api` on a REST path and left one
question open: whether a cloud session can reach `search/issues`, which is
REST but not under `repos/`. The first guarded retro routine on that base
settled it. `trimtab ingest --dry-run --ledger-from-labels` failed on its
first call:

> gh: This GitHub API path is not available: sessions are bound to their
> configured repositories. Use repository-scoped endpoints
> (repos/{owner}/{repo}/...). (HTTP 403)

A cloud session reaches only `repos/{owner}/{repo}/...`, and only for the
repositories the routine names as sources. Every other path is refused:
`search/`, `user`, `orgs/`, `notifications`, as well as `graphql`.

The base used `search/issues` in four places: the ingest watermark query and
the ledger rebuild (both in `GhSource`), the conformance command's list of
PRs merged in the window, and the retro and upstream-retro commands' check
for a PR already titled `[<ID>]`, run once per ID.

The pulls endpoint, the obvious replacement, filters by neither label nor
merge date. ADR 0019 rejected it for that reason: reading every closed PR
since the adoption grows without bound with the repository. Measured on one
consumer: 230 PRs closed in 30 days, about 55 a week. A scan from a fixed
adoption date would reach 1000 rows in about four months.

Constraints: ADR 0005's watermark (the label) and ADR 0006's bounded reads
keep their meaning; the dry-run and `--apply --confirm` gates are unchanged;
no new dependency.

## Decision

- **Every GitHub call the base makes or prescribes is `gh api` on a
  `repos/{owner}/{repo}/...` path.** `GhApi` refuses any other path before gh
  runs; the `graphql` refusal is now a case of it. A path with a `.` or `..`
  segment, after percent-decoding, is refused too, since the server may
  normalise it out of `repos/`.
  A static test fails if a
  command, agent or template calls `gh api` on another path.
- **Merged PRs come from `repos/<r>/pulls?state=closed`**, filtered by merge
  time and label in the package. A dated read sorts by `updated` descending
  and stops at the first page whose last row was updated before the date: a
  PR is updated when it merges, so none merged since the date lies further
  on. An undated read (a backfill) sorts by `created` ascending and stops once
  it has `--limit` unlabelled merged PRs.
- **The scan is bounded and loud.** It reads at most 1000 closed PRs
  (`SCAN_LIMIT`, 10 pages); a scan that would need more is an error, not a
  cut-off, since a PR it did not reach could be one it needs. A `--limit` or
  `--history-limit` above the bound is refused before any call.
- **An ingest reads PRs merged in the last 2 weeks** (`--ingest-weeks`,
  default 2), never before the adoption: the floor is the later of the two.
  The report prints the floor. `--ingest-weeks 0` reads from the adoption,
  and `--backfill` still reads from the start. The evidence window
  (`--window-weeks`, 8) is unchanged.
- **One run reads the listing once.** The ledger rebuild (8 weeks) and the
  ingest (2 weeks) share one scan: a narrower floor is served from a wider
  scan already read.
- **The commands follow.** Conformance reads closed PRs most recently updated
  first, back to the window start, at most 10 pages, and says the count is
  partial when it stops there. The retro and upstream-retro read open PRs and
  closed PRs updated within the window once per run, not once per ID, and
  match `[<ID>]` titles themselves; a listing stopped at 10 pages cannot rule
  a PR out, so no draft opens that run.

## Options considered

- **Keep `search/issues` and have the environment allow it.** Rejected. The
  base does not control which paths a cloud session allows, and the refusal
  names repository-scoped endpoints as the way.
- **Search in interactive sessions, the pulls endpoint in the cloud.**
  Rejected for ADR 0019's reason against a fallback: two paths, one exercised
  only where a surprise costs most, and a fallback degrades silently where a
  failure should be loud.
- **Scan from the adoption date, as the watermark query did.** Rejected. Exact,
  but every run re-reads every closed PR since adoption; at the measured rate
  it reaches the bound in about four months, and the remedy (moving the
  lock's adoption date) is an edit to the lock to keep a loop running.
- **Floor at the evidence window (8 weeks).** Rejected. About 440 rows a run at
  the measured rate, four times the 2-week scan, mostly to re-read PRs a
  weekly run has already labelled; evidence counts for 8 weeks, but a PR
  needs ingesting only once.
- **Floor at the merge time of the newest labelled PR.** Rejected. Ingest
  runs oldest first, so that PR marks how far it got, but a PR left unlabelled
  on purpose (a newer block version, ADR 0007) sits below it and would never
  be read again.
- **List labelled PRs from `repos/<r>/issues?labels=<label>`.** Rejected. It
  filters by label on the server, but the rebuild and the ingest would then
  read two endpoints with two shapes; one scan of the pulls serves both.

## Consequences

- The retro, conformance and upstream-retro routines can make every GitHub
  call they need in a guarded cloud session.
- Accepted: a PR left unlabelled for more than 2 weeks (two missed weekly
  retro runs, or a backlog above `--limit` for that long) is never ingested
  by a default run. The printed floor makes the gap visible, and a run with a
  wider `--ingest-weeks` reads it.
- Accepted: a repository that closes more than 1000 PRs in 8 weeks cannot
  rebuild its ledger, and one that closes more than 1000 in 2 weeks cannot
  ingest, until the bound is raised. That is loud by design. With
  `--ledger-from-labels` (every routine) the 8-week rebuild runs first, so it
  is the binding scan: past its bound the run stops before ingesting
  anything, even when the 2-week ingest scan alone would fit. Raising the
  bound, not the ingest floor, is the remedy.
- Accepted: a PR updated while a scan is paging moves to the front and shifts
  the pages. The scan counts a PR seen twice once, but can miss the PR that
  moved; it is updated, so the next run reads it.
- The per-ID PR searches in the retro commands were N+1; one read now serves
  every ID.
- `search/issues`' own rate limit (30 a minute) and 1000-result cap no longer
  apply; the scan costs at most 10 core-API calls.

## Trial, still open

Closed 2026-10-16: see "Trial result" below.

The first guarded cloud routine run on this base: `ingest --dry-run
--ledger-from-labels` and the conformance command complete with no 403, and
the run log shows no `gh api` call outside `repos/`. The result is recorded
here.

## Trial result (2026-10-16)

Two manual runs of per-project routines on a base carrying this decision and
its successors, each with its guards live: a retro (2026-10-15) and a
conformance review (2026-10-16). Both whole logs were read on 2026-10-16.

The retro:

- `trimtab ingest --dry-run --ledger-from-labels`, `ingest --apply` and
  `propose --apply` ran end to end with no 403 and no failed `gh` call. The
  rebuilt ledger covered every labelled PR merged in the window, and the
  report printed the ingest floor.
- Every `gh api` path shown in the log was under `repos/`, including
  `repos/{owner}/{repo}` once. The log cuts some long commands short, so not
  every path is shown, and the CLI's own calls are not logged one by one;
  `GhApi` refuses any other path before gh runs, and nothing was refused.
- A separate shell command, chaining a push with the PR create, was refused
  by the guards for an unrelated reason; it made no GitHub call.

The conformance review:

- It listed the window's merged PRs from the closed-pulls listing, read
  each one's body, reviews and check runs, opened its report as a draft PR
  and wrote the body back, end to end, with no 403 and no failed `gh` call.
- Every `gh api` path shown in the log was under `repos/`, including
  `repos/{owner}/{repo}` once, with the same caveat on long commands cut
  short. The only refused call was the guard probe the prompt requires.

Result: passed. No path outside `repos/` was needed for either command.
