# 0063. The plan-run enforcement gate

## Status

Proposed (2026-11-11). Accepted when the PR that adds it merges. Designs this base's side of the
doctrine record `0027-plan-runs-own-their-pr-to-merge.md` in Trimtab (called "the doctrine
record" below, so that it is not read as this base's ADR of the same number): its §Enforcement
rows E5–E21 and E23, this base's job for E22, and the E20 command. It settles the doctrine
record's "Requirements the later PRs must settle". Every row here keeps or tightens its row's
text; none loosens one, and none changes a row's owner.

Lands in four PRs, each citing this record: (1) this record, the ledger, E20 and E21; (2)
guard-github's E12, E14–E17 and E19; (3) the plan-run layer's E5–E11, E13, E18 and E23, and the
E22 workflow template; (4) `wrap-merged`. Two changes are security controls (ADR 0011) and go in
PRs of their own, made by the operator: registering the review recorder (E11) in
`settings.base.json`, and a consuming project adopting the E22 workflow. Until each lands, E20
reads its row as not shown, so no run starts.

## Context

The doctrine record moves every plan-run mechanism out of rule text into rows that a hook,
permission rule or test enforces (NRM-3), and makes AGW-1's authorisation check fail until the
E20 command shows each row. This base owns most rows. The record left six questions to this base:

- E19: the secret scanner may be a check-run App the agent's token cannot read.
- E13: what record, which no agent can write, shows that a push was made under E14.
- E8: the key that matches a finding to a review of the merge-base.
- E12: a deterministic test for "answers a failing check".
- K+1: a durable source for the highest part number, since branch-update events expire.
- E22: the consuming project's approver must read the job.

Constraints in force:

- A consuming project's agent token reads Actions job records and logs, review comments and PR
  state. It does not read check runs, and it cannot re-run a job.
- GitHub is reached through repository-scoped REST only (ADR 0021). Review-thread resolution is
  GraphQL-only, so this base never reads it directly.
- A hook that outlives its timeout fails open. Every guard stops itself inside 6 s and blocks
  (ADR 0055).
- Guards keep their state under `<config dir>/guard-state/` (ADR 0028), which guard-paths refuses
  to agent writes by its physical path (ADR 0029).
- guard-paths' plan-run layer does not confine Bash file writes (ADR 0041). A push is the only
  way such a write leaves the machine, and guard-github reads the push.

## Decision

### 1. The ledger

`<config dir>/guard-state/plan-runs/<sha256 of owner/name>/<plan>/item-NN-partK.jsonl` is
append-only, and only hooks write it. Each line is one event: `push` (branch, old and new SHA,
`TRIMTAB_PLAN_RUN` value, time), `round` (head, the passes recorded on it, whether it was
before the draft), `draft` (PR number, head), `ci-fix` (head, what it answers), `rerequest`
(head, reason), `stop` (reason). It is machine-local. A run that finds a `plan/…` branch on the
remote with no ledger for its part stops: a cloud session cannot resume another machine's
branch. This is tighter than the row, which is allowed. The forgery residual is the one ADR 0028
accepts for its list: a Bash command that hides the config dir's path from guard-paths. E22
below rechecks every row that rests on server facts, so a forged ledger buys at most the
ledger-only rows (E10, E11, E12, E23), which are named under Consequences.

### 2. E20 — `trimtab check-enforcement`

`trimtab check-enforcement [--repo DIR] [--server]` exits 0 only when every row is shown.

- **Rows come from the doctrine record, never the manifest.** It reads the lock's `trimtab_sha`,
  finds the PR that added the doctrine record to Trimtab's default branch, and reads the file at
  that PR's merge commit, which the Trimtab admin merged and from which the lock's commit
  descends. It parses the §Enforcement table into exactly E1–E23, each with its condition,
  fact and owner text. A missing, extra or duplicate row, or any failed read, fails.
- **Owners decide the source.** "Guard", "Guard (plan-run layer)", "guard-github" and
  "trimtab-core" rows are this base's. "Consuming project" rows are the project's. E22 is both.
- **A base row is shown** when `trimtab/enforcement/manifest.json` at the pinned base maps it,
  and all of these hold:
  - the entry's `row_sha256` equals the SHA-256 of the row's text as merged, so a changed row
    is a stale entry until this base records the change;
  - its hook is one the owner names (guard-paths for "Guard (plan-run layer)", guard-github for
    "guard-github", either for "Guard");
  - its tests exist and are among those `lint-test` runs, which a contract test here pins;
  - every attempt of `lint-test` in `.github/workflows/ci.yml`, on the `push` event, for the
    pinned base commit, passed, read from trimtab-core's job records;
  - the pinned base descends from the commit that added the entry;
  - outside `--server`: the hook is installed from the pinned base and registered live, by the
    ADR 0004 liveness check.
- **A project row is shown** when the instance file's `enforcement` key names a workflow path
  and job for it, and every attempt of that job on the `push` event for the default branch's
  latest commit passed, read from the project's job records. A row the file does not name is not
  shown.
- **Output** is one line per row (`E7 shown` or `E7 not shown: <reason>`), never a value read
  from a file. A pass is cached at `guard-state/enforcement/<sha256 of owner/name>.json` under
  the key (lock SHA, pinned base SHA, default-branch head). Only a pass is cached.

The manifest maps each row to its refusal tests. A mutation test replaces each row's decision
function with one that allows, runs that row's refusal tests, and fails unless they fail. So
"each test fails with its guard removed" is itself a test in `lint-test`.

### 3. E21 — guards obey the gate

In a run session (hooks/session-mode.sh), guard-paths refuses every Edit, Write, MultiEdit and
NotebookEdit, and guard-github refuses every GitHub write and every `git push`, unless the cache
holds a pass for the current lock, pinned base and default-branch head. The head is read by
`git ls-remote` under a 3 s timeout, on every guarded write. There is no time-to-live, so the
result is never stale. A timeout, a missing cache or a mismatch refuses. The session-start hook
runs the full command in a run session to fill the cache. Bash file writes stay unconfined
(ADR 0041), but they reach GitHub only through a push that this row refuses.

guard-github's prefilter grows to read `git push` and remote ref writes, which it now ignores.

### 4. The consuming project's facts (E2, E3, E4)

The base reads what the project's rows provide. Their format is fixed here, so the project's
workflow and this base's readers agree. The instance file's new `review` key names:

- the E2 review job (workflow path, job name, event);
- the E3 facts job, which must be a **separate job** with no model step: tighter than E3's
  "a step after the model step", because the base cannot tell a step's log lines from the model
  step's output in the same job;
- the review's login;
- the re-request label;
- the outcome tags (fixed, wrong, intentional, moot, deferred);
- the review bound (default 60 minutes) and the watch bound (default 24 hours).

The facts job prints, and the base reads from its log only these lines:

```
TRIMTAB-REVIEW v1 run=<run id> head=<sha> tree=<sha> verdict=<pass|fail|fail-closed>
TRIMTAB-THREAD v1 run=<run id> id=<root comment id> severity=<critical|high|medium|low> state=<open|resolved> resolved_by=<login|-> path=<path> line=<n> category=<slug> key=<sha256>
```

A line in any other shape, a version other than `v1`, or two `TRIMTAB-REVIEW` lines fails the
read. `key` is defined in E8.

### 5. Rows E5–E19 and E23

Each refusal is a `trimtab` subcommand, called by the hook named in its row and testable on its
own. The ready mark (`gh pr ready`, its REST spelling, and GraphQL `markPullRequestReadyForReview`)
goes through `trimtab plan-ready-check`. The re-request (adding the named label in any spelling)
goes through `trimtab plan-rerequest-check`. A `plan/*` push or ref write goes through
`trimtab plan-push-check`. Every failed read refuses.

- **E5.** Every review comment by the review's login on the PR whose root id is not in an E3
  line for the PR refuses ready and writes a `stop`.
- **E6.** A re-request refuses unless the head is not one an E2 run has reviewed. The one
  exception is the latest verdict on this head being `fail-closed` with no earlier `rerequest`
  for this head in the ledger.
- **E7.** Ready refuses unless an E2 run on the exact head has an E3 `pass`. The alternative is
  a head whose diff from a head so reviewed adds only new files in the plan's register folder,
  each named in an E3-listed thread's deferral reply, plus added lines in that PR's progress
  record. Each such line holds nothing but a Markdown link to one of those files: tighter than
  "link lines".
- **E8.** Ready refuses unless every E3 thread has a reply (`in_reply_to_id` its root) by the
  agent login whose first word is one of the instance's tags. A CRITICAL or HIGH thread accepts
  only the fixed tag. A deferral tag is accepted only below HIGH, only when the thread's line is
  outside the head side of `git diff <merge-base>..<head>`, and only when an E3 thread with the
  same key appears in the facts of an E2 run whose `tree` equals the merge-base's tree.
  **The key** is the SHA-256 of the path, the category, and the anchored line's text with
  whitespace collapsed, read at the reviewed commit. The facts job computes it, and this base
  recomputes it at the merge-base and compares. Requiring the merge-base's exact tree, rather
  than any earlier review, is the strict reading of "a review of the merge-base".
- **E9.** A fixed reply cites a commit by full SHA. The commit descends from the thread's
  commit, is on the branch, and changes at least one line in the thread's diff hunk. A thread
  counts as closed only when a later E3 line for its id says `state=resolved`. A resolution by
  the agent login does not count. guard-github already refuses that resolve (ADR 0037).
- **E10.** A re-request refuses unless the ledger holds a complete round (all three passes) on
  a head H at or after the last commit answering a CI review or failing check, and the branch
  holds at most one `push` after H. That one push is the round's own fix. Allowing exactly one
  push is the deterministic form of "commits fixing that round's own findings".
- **E11.** A PostToolUse hook on the Skill tool appends a `round` entry when `trimtab-review`,
  `security-review` or `code-review` completes. The entry names the checkout's `HEAD` at the
  call. A round is complete when all three are recorded on one head. A slash command the
  operator types, or a pass run any other way, records nothing, so it counts for nothing. The
  hook's registration is the operator's control PR.
- **E12.** At each `plan/*` push after a `draft` entry, `plan-push-check` decides whether the
  push answers something. It answers a failing check when any job record on the PR's previous
  head concluded `failure`, `timed_out` or `cancelled`. It answers a CI review when an E3 facts
  run on the previous head has no later push. Either case adds one `ci-fix`, counted once
  however many it answers. The exclusions are the draft-opening run, the one E6 re-request, the
  E7 register commit, and the round's own fix push (E10). A push that would make a fourth
  `ci-fix` for the part refuses and writes a `stop`. "Answers a failing check" is a pure
  function of the job records and the ledger, tested on recorded fixtures.
- **E13.** `trimtab plan-resume-check` passes only when four things hold. Every commit in
  `origin/<default>..origin/<branch>` lies inside a ledger `push` with `TRIMTAB_PLAN_RUN=1`.
  The repository's branch-activity events (`GET /repos/{owner}/{repo}/activity`, filtered to the
  ref) form an unbroken chain from the merge-base to the head, each by the agent login. The
  ledger's pushes and the events agree. The diff from the merge-base is in scope by
  `planscope`. A page limit reached, an event older than the earliest one returned, or a failed
  read is a stop. **The record no agent can write** is the pair: the ledger shows E14 allowed
  the push, and the server's event shows who made it. Neither alone suffices. The plan-run
  layer refuses every write on a branch whose resume check has not passed for its head.
- **E14.** A `plan/*` ref write refuses unless `TRIMTAB_PLAN_RUN=1` is in the hook's
  environment. That covers `git push` to it (an explicit refspec, or a bare push resolved by
  `git rev-parse --symbolic-full-name @{push}`, where failing to resolve refuses), a REST ref
  create or update, and a contents write on it. Allowed pushes are recorded in the ledger.
- **E15.** A non-fast-forward write to `plan/*` refuses: `--force`, `--force-with-lease`,
  `--force-if-includes`, a `+` refspec, `--mirror`, or a REST ref update with `force`. This
  is tighter than guard-bash's ADR 0036, which allows `--force-with-lease` on a PR branch.
- **E16.** `gh pr close` and a REST state change to `closed` refuse when the PR's head is
  `plan/*`, which a lookup decides. A failed lookup refuses. A ref delete of `plan/*` refuses
  in any spelling, including `gh pr merge --delete-branch`.
- **E17.** A `plan/*` push refuses when the branch's open PR is not a draft. The exception is a
  push whose diff has the E7 register-commit shape.
- **E18.** A `plan/*` push refuses when its diff from the remote head does any of these:
  - adds a line holding a marker from the project's list (the instance's `fix_limits.markers`,
    a file of literal strings, with defaults for the common Python and Actions skip and
    suppression markers);
  - deletes a test file, or removes more test definitions from one than it adds;
  - lowers a number on a line matching the project's `fix_limits.thresholds` patterns.

  The exception is the authorised plan's item section (from `planauth`'s plan text) naming that
  exact marker or threshold line.
- **E19.** The instance's `secret_scan` key names an Actions job: workflow path and job name.
  A check-run App such as GitGuardian cannot be named, because the agent's token cannot read
  it. That App remains the operator's own merge gate. At each guarded write, the job's records
  are read for the latest pushed head:
  - a failing conclusion or an unreadable result writes a `stop`;
  - while the stop stands, the only writes allowed are a push touching only that part's
    progress record, adding `plan:blocked`, and removing `plan:continue`;
  - pending allows writes but refuses ready.

  With no `secret_scan` key the row is not shown, so no run starts.
- **E23.** Opening a PR from `plan/*` refuses unless the ledger holds at least one complete
  round on the exact head and at most five pre-draft rounds. A sixth pre-draft round refuses at
  its first pass's PreToolUse.

### 6. K+1 — the part number

The highest part K for an item is the maximum over three sources: the progress-record file
names on the default branch; the head refs of every PR in the repository (`state=all`, which
closed PRs keep and events do not); and the ledger. A failed read of any source refuses a new
part.

### 7. E22 — the server-side job

This base ships `templates/workflows/plan-gate.yml`. The project adopts it at
`.github/workflows/plan-gate.yml`, job `plan-gate`, on `pull_request` from `plan/*`. That is the
operator's control change. It runs two commands on the PR head:

- `trimtab check-enforcement --server`;
- `trimtab plan-ready-check --server`, which re-checks every row resting on server facts: E5,
  E7, E8, E9, E14 and E15 through the activity events, E16, E18 on the whole diff, and E19.

The approver reads the job like `plan-scope` (ADR 0058): every attempt on the head passed, from
the pinned path and the `pull_request` event. Until the approver reads it, only the admin merges
a run's PR, as the doctrine record says.

## Options considered

| Option | Why it lost |
|---|---|
| Keep counts and push records on GitHub (labels, comments, commit statuses) | The agent's token can write every one of them |
| A ledger with an HMAC | The key would sit where the agent's shell can read it; the protection is still guard-paths, so it adds code and no fact |
| Cache the E20 pass with a time-to-live instead of keying it to the default-branch head | A merge to the default branch inside the window would run under a stale gate |
| Run the full E20 command in every hook | Dozens of API reads per write; it outlives the 6 s deadline, and a timeout fails open |
| Read E3 facts from the review job's own log | The model step writes to the same log and could print a facts line |
| Read thread resolution by GraphQL | ADR 0021: REST only. The facts job, which runs with the project's token, reports state instead |
| Defer on any earlier review that saw the same line | Looser than "a review of the merge-base"; rows may only tighten |
| Scope deferral by touched path | Rejected by the doctrine record; pulls old findings into the item |
| Allow any commits after the round under E10 | Cannot tell a fix commit from new work; "one push" is the deterministic bound |
| Count a CI-fix round by commit message | The agent writes commit messages |
| Name the GitGuardian check run as the E19 scanner | Unreadable by the agent's token, so every run would stop forever |
| Read the part number from branch-update events alone | They expire, and a deleted branch would reset counts |
| Put E14–E17 in guard-bash, which already reads pushes | The rows name guard-github as owner, and E20 checks the owner; guard-github reuses guard-bash's push reader |
| Ship E22 as a workflow in this repository | This base runs no plans; the job must run where the PR is, so it is a template the project adopts |
| One PR for every row | Too large to converge in three-pass review |

## Consequences

- No plan run starts until every PR here, the operator's recorder registration, and the
  project's rows (E1–E4, the E19 job, the E22 workflow, the `enforcement` and `review` keys)
  all land. This is by design.
- A run is tied to the machine that started its part. A cloud run cannot resume a local one, and
  the reverse. Lost guard state (a new machine, a wiped config dir) stops a part, and only the
  operator's branch deletion starts part K+1.
- Deferral needs a CI review whose tree equals the merge-base's. A project that wants deferrals
  to be possible needs a review of each default-branch head. Without one, every out-of-scope CI
  thread stops the run. Accepted as the strict reading.
- E10, E11, E12 and E23 rest on the ledger only. E22 cannot recheck them server-side. A session
  that forges its ledger past guard-paths could skip in-session rounds, but it could not change
  what CI review, the E22 job or the approver see.
- Every guarded write in a run costs one `git ls-remote`.
- The instance schema grows by `enforcement`, `review`, `secret_scan` and `fix_limits`. An
  instance without them shows no project rows.

## Measurements

None. The 3 s `ls-remote` bound and the defaults are unmeasured starting values. The 60-minute
and 24-hour bounds come from the doctrine record.
