# 0054. CI runs each guard matrix pass once on a pull request, and the unit tests by module

## Status

Accepted on merge. A security control (SEC-7): the operator authorised changing
`.github/workflows/ci.yml` in chat, for steps 4-5 of the CI-time plan, in a PR of
its own. One record holds both decisions because they change one job's steps
under one authorisation, against the same measurements.

## Context

`lint-test` is one serial job and took about 7-11 minutes on a PR. Step seconds
over 11 runs (`pull_request` 7, `push` 4), medians:

| Step | PR | Push |
|---|---|---|
| bootstrap env test | 159 (88-176) | 150 |
| base expectations, python3 fallback | 106 (73-114) | n/a |
| guard matrix, python3 fallback | 102 (75-116) | 100 |
| base expectations, jq present | 90 (65-95) | n/a |
| guard matrix, jq present | 90 (65-94) | 84 |
| trimtab tests | 44 (31-50) | 44 |

Total: PR 629 (415-661), push about 395 (378-433).

The constraints:

- Coverage may not drop across a PR's run and the push that follows its merge.
  Both parsers must be proved, and removing jq, where it runs, stays last in the
  job. What moves from the PR's gate to the push is named under Consequences.
- `lint-test` is one required check and is never skipped (ADR 0053), so workflow
  `paths-ignore` is out and any condition has to be on a step.
- The base expectations exist to turn a guard behaviour change red when a PR has
  edited the matrix to agree with it. They run before any PR code, and that order
  is what protects them.
- Unit tests must stay deterministic, so concurrency needs no shared state.

Two cheaper changes needed no control and landed first: the bootstrap checks run
four at a time, and the matrix's payload builder stopped starting python3 per
case. On one CI run each, the bootstrap test went from 151 s to 67 s and a matrix
pass by 18-20 s.

## Decision

**When a pull request leaves the git tree entry of `tests/guard-tests.sh` (mode and
blob) the same as the base branch's, the later `guard matrix: jq present` and
`guard matrix: python3 fallback` steps do not run, nor does `remove jq`.** The
comparison is made in the step that writes the base file, before any PR code has
run. Only an exact `true` skips. A different entry, no entry on the PR's side, or
that step failing all run every pass. The base expectations always run on a pull
request. A push has no base run, so it always runs both `guard matrix` passes.

Tree entries rather than `cmp` on the files, because bytes miss two changes: a
`chmod -x`, and a symlink to the base copy the step has just written. Under `cmp`
the first would be red only on `main`, and later PRs would stay green over it.

In that case both pairs run the same bytes over the same hooks in the same modes.
The base steps set `CLAUDE_CONFIG_DIR`, but the matrix overwrites it before its
first case, and the script has no dependence on its own name.

The evidence: on 7 PR runs that left the file unchanged, the base and PR verdict
lines were identical in both modes once temp-directory names are normalised (1468
and 1494 lines on older heads, 1535 and 1570 on newer). Of the last 60 merged PRs,
36 left the file unchanged, and all 22 that touched `hooks/` also edited it.

**`trimtab: tests` runs module by module, four at a time.** The sum of `Ran N
tests` must equal the loader's `countTestCases()`, so a module that stops early or
never starts goes red without pinning a number that goes stale. Locally on 4 CPUs
this took the step from 81 s to 28-34 s over 5 runs, with all 1177 tests run and
per-test verdicts identical to a serial run. The loader's count took under a second.

Each module has 10 minutes, far past the slowest. `xargs -P4` holds a module's
output until the end, so without a bound a hung module would be killed with the
job and its log lost with the runner. With the bound it goes red, its name is
printed and its log survives.

### Expected saving

Estimated, scaled from local ratios and these one-run CI numbers. Step 8 of the
plan replaces it with a measurement. The later pair cost 169 s in #136's run, on
about 60% of PRs. The unit tests save about 25-29 s on every run, taking the local
58-65% against the 44 s median; a runner's 4 vCPUs may scale worse.

## Options considered

- **Skip the base expectations instead.** That is the option the work started
  from. Rejected: it leaves only the passes that run after PR code. Skipping the
  later pair keeps the order-protected one, and gives up only what Consequences
  names.
- **Compare the files with `cmp`.** Tried first. Rejected for the two changes it
  misses, above.
- **Skip by a list of paths the matrix does not read.** Rejected. The matrix reads
  `hooks/`, `settings.base.json`, `rules/`, `systemd/` and the trimtab package; a
  stale list fails open; and it saves little beyond byte-equality.
- **Workflow `paths-ignore`.** Rejected: a skipped required check can count as
  passing.
- **Run the base and PR passes concurrently in phases.** Measured: 75.1 s for two
  at once against 67.5 s for one. It would save about 170 s on all PRs, but only
  about 68 s on average beyond this decision, and PR code would run beside the
  protected base run. Deferred; reopen it if PRs that edit the matrix stay slow.
- **A shadow PATH instead of removing jq, so all four run at once.** Measured:
  87.6 s for four. Rejected: less faithful to a machine without jq.
- **Job-level `timeout-minutes` instead of a bound per module.** Rejected: it kills
  the step before the logs print, so a hang still names nothing, and the job has no
  credible limit yet while its run time is moving. Reopen it after the timings
  settle.
- **Stream module output as it arrives.** Rejected: four modules' lines interleave,
  and the bound above already names a hung one.
- **Shard the matrix by case.** Rejected: 3733 lines of sequential fixtures, and
  the base file cannot be restructured from a PR.
- **Shorten the 8 s hung-adapter case.** Rejected: it asserts the hook's own
  deadline, so it needs a hook change and a weaker assertion.
- **Make the guards cheaper.** Rejected: a hook change on a measured 6-16 ms each,
  to the product under test.
- **Separate jobs rolled up into `lint-test`.** Rejected: job billing, and it loses
  the ordering above.
- **Larger runners.** Rejected: paid infrastructure (ENG-5).
- **Move the unit-test driver into a script under `tests/`.** Rejected: testable,
  but it moves the count check out of the guarded workflow file.
- **Pin the test count in `ci.yml`.** Rejected: stale on every PR that adds a test.
- **`pytest-xdist` or GNU parallel.** Rejected: a new dependency (ENG-3) where
  coreutils and the standard library are enough.

## Consequences

Accepted, on a PR that leaves the matrix file unchanged. The merge gate gives up
these; the push still has them, but a push is red on `main` after the merge and
blocks nothing:

- `apt-get remove` does not run. The base pass hides jq by `mv`; to `command -v`
  the absence is the same.
- The matrix does not run after bootstrap, so a dependence on that state shows
  only on the push.

Order protection is not reduced: the skipped pair ran after PR code could already
have tampered with the runner. `lint-test` still runs and reports, so ADR 0053's
rule holds at job level.

A PR that edits the matrix gets no saving from the skip, and those include every
PR that changed a hook in the sample. The cost of the unit-test change is that a
module's output appears only once all modules have ended, up to 10 minutes after
one hangs. A hang in any other step still runs to the job's default limit, as it
did before.

## Trials

Negative controls on real PRs are recorded here before merge.
