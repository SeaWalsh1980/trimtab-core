# 0063. The plan-run enforcement gate

## Status

Proposed (2026-11-11). Accepted when the PR that adds it merges. Designs this base's side of the
doctrine record `0027-plan-runs-own-their-pr-to-merge.md` in Trimtab (called "the doctrine
record" below, so that it is not read as this base's ADR of the same number): its §Enforcement
rows E5–E21 and E23, this base's job for E22, and the E20 command. It settles the doctrine
record's "Requirements the later PRs must settle". Every row here keeps or tightens its row's
text; none loosens one, and none changes a row's owner.

Lands in four PRs, each citing this record: (1) this record, E20 and its recorder; (2)
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
before the draft), `draft` (PR number, head), `rerequest` (head, reason), `stop` (reason). It
is keyed by repository, plan and part, never by session, so a later session on the same machine
and config dir reads the same ledger. It is machine-local, and a cloud session's ledger ends
with its container.

Only the rows that no server fact can show read it: E10, E11 and E23 (rounds) and E13 (that
each push was made under E14). E12's count is taken from server facts (§5), so it survives any
hand-over. A hook never starts a ledger for a part whose branch already exists on the remote:
a fresh ledger would vouch for pushes it never saw.

A run with no ledger for its part acts under §1a or stops. This is tighter than the rows, which
is allowed. The forgery residual is the one ADR 0028 accepts for its list: a Bash command that
hides the config dir's path from guard-paths. E22 below rechecks every row that rests on server
facts, so a forged ledger buys at most the ledger-only rows (E10, E11, E23), which are named
under Consequences.

### 1a. Watch-only: a session without the ledger, after ready

A run session whose part's PR is open and **not a draft**, and which holds no ledger for that
part, is watch-only. The plan-run layer and guard-github then allow:

- reads;
- PR comments;
- replies to review comments, opening with the `wrong`, `intentional` or `moot` tag (E8 still
  applies at E22);
- adding `plan:blocked` and removing `plan:continue`.

They refuse:

- every file write, push and ref write;
- returning the PR to draft;
- a re-request;
- a reply opening with the fixed or deferral tag. Both need a push: a commit for fixed (E9), a
  register entry for deferral (E7).

So a check that fails after ready, or a thread that needs a fix or a deferral, is a stop in
watch-only. The operator restarts the part, or a session with the ledger takes it back. E19 and
E21 apply unchanged. With a ledger, the full rules apply instead.

### 1b. Hand-over at part boundaries

Context rot is bounded by part size, never by moving a part's state between sessions. A session
hands over only at these points:

- **A part boundary.** The part's PR merges with `## Status` `item NN part K complete; item
  continues`, and a fresh session starts part K+1 from `## Next`, with a new ledger.
- **After ready**, into watch-only (§1a).
- **Mid-part before ready**, only to a later session on the same machine and config dir.

A session that expects not to finish its part before ready ends the part early at a clean
point (in-scope work done so far, register entries for the rest of the part's scope within the
item), rather than handing over mid-part. Where that cannot be done, it stops. `wrap-merged`
sizes parts to fit one session and names when to end one early. That is guidance, not
enforcement: a part too big for its session costs a stop, never a weaker gate.

### 2. E20 — `trimtab check-enforcement`

`trimtab check-enforcement [--repo DIR] [--server]` exits 0 only when every row is shown.

- **Rows come from the doctrine record, never the manifest**, and every input is a git object
  named by a SHA that only an admin's merge sets:
  1. **The lock.** It is read from the project's default-branch head, as fetched, after checking
     that head against the server, never from the working tree. The lock moves only by an
     admin-merged PR.
  2. **The record's path.** It comes from the instance file at the lock's `trimtab_sha`, as an
     object in the instance checkout.
  3. **The record's text.** It is the record at the latest first-parent commit of `trimtab_sha`
     that added it: the merge that put the current file on Trimtab's default branch, so the text
     "as merged". A later edit to the record is not read.
  4. **The table.** The §Enforcement table is parsed into exactly E1–E23, each with its
     condition, fact and owner text. A missing, extra or duplicate row, or any failed read,
     fails.

  No Trimtab API read is made. A project's agent token cannot read a private instance, and a SHA
  cannot be forged, so the git objects are a stronger fact than the API would give. The admin's
  identity rests on the project's lock PR, not on Trimtab's merge.

  A SHA names one object only while nothing redirects git, and git does not re-hash an object
  file it reads. So:
  - **Verified reads.** Every commit, tree and blob a decision rests on is read through
    `git cat-file --batch`, and its SHA-1 is recomputed here. History is walked here, from
    verified commits, never by `git log`. So a substituted object file, a replace ref or a graft
    changes nothing a SHA reads as. One long-lived `git cat-file --batch` per repository serves
    every read (ENG-6.2). Commits are parsed as git parses them: the first header line names the
    tree, then only consecutive `parent` lines count.
  - **Refused repositories.** A repository is refused when it has alternates or grafts, when it
    is configured as a partial clone (`extensions.partialClone` or any `remote.*.promisor`), or,
    except for the clean fetch, when its history is shallow. A partial clone matters because it
    fetches any missing object by running the transport its config names. A linked worktree is
    read through its common dir, which `--git-path` resolves.
  - **git itself** is the system's (where it resolves: root-owned, in a root-only directory).
    - It runs with an environment built from an allowlist: no steering or config variables, no
      global or system config.
    - Replace objects are off, and lazy fetch is off (`GIT_NO_LAZY_FETCH`).
    - planscope's pinned settings apply, plus `protocol.allow=never`, an empty
      `core.sshCommand` and no hooks. These pins are a further layer only:
      `GIT_NO_LAZY_FETCH` and the partial-clone refusal are what stop a lazy fetch, because a
      repository's `protocol.<name>.allow` outranks `protocol.allow`. Tests show each of the
      env setting and the pins stopping the planted fetch alone, and the fetch running with
      both removed.
    - Proxy variables are kept, so a proxied host still reaches GitHub. Alone, a proxy can only
      make a read fail, since TLS is verified.
    - A CA variable is kept only when the file or directory it names, and every directory above
      it, is root-only. It is kept as the resolved path, the one judged, so a session-owned link
      to it cannot be re-pointed afterwards. A CA the session could write would let a proxy
      forge the API.
    - Each object read has a 10 s deadline and a 64 MiB cap, so a FIFO or a forged size fails
      the read rather than hanging it.
  - **gh never reads its own config.** The token is taken once, from `GH_TOKEN` or
    `GITHUB_TOKEN`, else `gh auth token -h github.com` (with the session's D-Bus and runtime dir,
    so a keyring-held token is found). That one read uses the user's gh config and can only pick
    the token; a forged token only fails the reads. Every gh call after it runs with a fresh,
    empty `GH_CONFIG_DIR`, `GH_HOST=github.com` and that token. A session-written gh config (a
    unix socket, another host, in any YAML form) takes no part in those calls.
- **Owners decide the source.** "Guard", "Guard (plan-run layer)", "guard-github" and
  "trimtab-core" rows are this base's. "Consuming project" rows are the project's. E22 is both.
- **The pinned base** is the instance file's `base.sha` at `trimtab_sha`. The lock's derived
  `base_sha` must equal it, when the lock has one.
- **A base row is shown** when `trimtab/enforcement/manifest.json`, read as the object at the
  pinned base (never the working tree), maps it,
  and all of these hold:
  - the entry's `row_sha256` equals the SHA-256 of the row's text as merged, so a changed row
    is a stale entry until this base records the change;
  - its hook is one the owner names (guard-paths for "Guard (plan-run layer)", guard-github for
    "guard-github", either for "Guard");
  - its tests exist and are among those `lint-test` runs, which a contract test here pins;
  - every attempt of `lint-test` in `.github/workflows/ci.yml`, on the `push` event to `main`,
    for the pinned base commit, passed with a runner and steps, read from trimtab-core's job
    records;
  - the pinned base descends from the commit that added the entry. This holds by construction,
    because the manifest is read at the pinned base;
  - outside `--server`, **this checkout is the pinned base**, for every base row, hooked or not,
    since this checkout's code is what shows the row:
    - `HEAD` is the pinned base;
    - every file under `hooks/` and `trimtab/` hashes to the base tree's blob, with its mode;
    - no other file is there, ignored ones (a `__pycache__`) included;
    - no symlink is there.

    Hashing every file against a verified tree means no index flag (skip-worktree,
    assume-unchanged), stat trick, ignore rule or substituted object can hide an edit. A
    directory that cannot be listed is refused, since it could hold a `.pyc` Python still
    loads;
  - outside `--server`, the hook is live:
    - the config dir's `hooks/<file>` resolves to this checkout's file and is executable;
    - the config dir's `settings.json` registers it for the manifest's event and matcher. The
      command must name the config dir's own path. The `$HOME/.claude/hooks/…` form counts only
      when the config dir is `~/.claude`, since otherwise it runs a file no check looked at;
    - no settings file, the config dir's or the project's, sets `disableAllHooks`, and none is
      unreadable.
- **A project row is shown** when the instance file at `trimtab_sha` names, under
  `enforcement.projects.<owner/name>` (compared without case), a workflow path and job for it,
  and every attempt of that job, on the `push` event to the default branch for its latest
  commit, passed with a runner and steps, read from the project's job records. A row the file
  does not name is not shown. One memoised reader serves every row and this base's CI, so all
  are judged from one snapshot.
- **`--server`** runs in a project's CI, which has no instance checkout (ADR 0015). It reads
  the rows from a derived lock field that `trimtab bump` writes, as it writes `base_sha`. That
  field is designed and added with E22's PR, and until then `--server` fails.
- **Output** is one line per row (`E7 shown` or `E7 not shown: <reason>`), never a value read
  from a file. The command only reports; it never writes the pass the guards read.
- **The pass cache** is `guard-state/enforcement/<sha256 of owner/name>.json`. Its key is the
  lock SHA, the pinned base SHA, the default-branch head and an **install digest**. The digest
  covers:
  - the settings files;
  - where each hook link points;
  - the code and instance roots;
  - this checkout's HEAD;
  - the path, mode and bytes of every file under `hooks/` and `trimtab/`.

  **One snapshot.** These are read once, after every network read, into one snapshot. The
  checkout and liveness checks judge that snapshot, and the digest seals the same bytes, so
  nothing changed between a check and the digest can slip into a pass. Every part of the digest
  is length-prefixed. A guard re-reads the snapshot locally (some 70 small files), so an edit, a
  checkout of another commit, or a hook unlinked or unregistered after a pass voids it at the
  next guarded call. Any file it cannot read as a regular file (a FIFO, a link), and any
  repository layout it does not know (a reftable HEAD), makes no snapshot, so there is no pass.
- **Only the recorder writes the pass**: `trimtab.adapters.enforcement_record`.
  - **Startup.** In a run session, hooks/session-mode.sh does three things, in order:
    1. It starts a new **epoch**: a random value written to a private temporary file and moved
       over `.epoch` with `mv -fT`. That replaces a planted link, even one to a directory, rather
       than following it, and fails on a planted directory. If no new epoch can be written (or
       the trusted binaries are missing), it removes `.epoch` instead, so no recorder holding the
       old one can match it.
    2. It runs `clear` synchronously, bounded at 4 s inside the hook's own 10 s.
       - The clear removes **every** pass and status line, never choosing by the project's
         `origin`, which the session could aim elsewhere to spare its own project's pass.
       - It reads no git, and takes the cache lock (waiting at most 1 s for it).
       - If it fails or times out, a bash fallback removes every pass and status line.
    3. It starts `record` in the background with the operator's launch environment and that
       epoch on its argv. If no epoch could be written, it starts no recorder.
  - **No session-written program runs:**
    - the hook's shebang is `#!/bin/bash`, not `env bash`, which would take bash from the
      session-writable PATH before any line runs;
    - every function of the hook that the recorder depends on runs with `PATH=/usr/bin:/bin`,
      and the hook itself sets it when run as a hook;
    - `python3`, `setsid` and `timeout` must also resolve to root-owned files in root-only
      directories;
    - every interpreter the hook starts gets `-X pycache_prefix=/dev/null/…`, a path no `.pyc`
      can be under, so a planted `__pycache__` is never loaded (`-B` only stops writes).
  - **The record run.**
    - **The epoch first.** With no epoch, or a malformed one, it reads nothing and writes no
      pass. A recorder started without one on argv (E21's guards) takes the current epoch before
      it waits on the lock.
    - **The lock.** It holds one lock for its whole run.
    - **The order.** It removes the project's pass before any read, so a SIGKILL leaves none,
      then fetches and runs the gate.
    - **The write.** It writes a pass only when every row is shown **and** its epoch is the
      current one, checked before the write and again after it (a pass written while the epoch
      moved is removed).
    - **The pass carries its epoch.** A guard accepts a pass only when that epoch is the current
      one, so a pass from an earlier epoch is refused at once, with no window.
    - **So** a recorder an earlier session left running, or one still waiting on the lock,
      cannot write a pass a later session would accept.
  - **Every other way out** leaves no pass: an error, SIGTERM (raised as an exit, so the cleanup
    runs), or a repository it cannot name (then every pass is removed). Each way out tries to
    write a one-line status naming why. A status that cannot be written is lost; the pass is
    settled either way.
  - **A session that becomes a run mid-session** (it moves onto `plan/*`) gets no
    SessionStart clear. Its guards then find no pass for the current key, or one an earlier
    recorder made for exactly this key, and start a recorder (E21). Accepted: the earlier pass
    was made by the recorder for the same lock, base, head and install.
  - **The repository** comes from the project's `origin` as configured (not as `insteadOf`
    rewrites it), read without the object checks since it names nothing the gate then trusts,
    and the instance file must name it. It is session-writable: see Consequences.
  - **The fetch.** It goes to an explicit `https://github.com/<owner>/<name>.git`, built from
    that name, into a fresh bare repository in a private temporary directory, depth 1. Only
    pinned config applies: no global, system or repository config; only the https protocol; no
    `core.sshCommand`; gh (with the empty config and the token above) as the only credential
    helper; `fetch.fsckObjects`. The lock is read from there, as verified objects. Nothing a session wrote to any git config takes part, and
    the fetched tip must still be the server's head.

The manifest maps each row to its refusal tests. A mutation test replaces each row's decision
function with one that allows, runs that row's refusal tests, and fails unless they fail. So
"each test fails with its guard removed" is itself a test in `lint-test`.

### 3. E21 — guards obey the gate

In a run session (hooks/session-mode.sh), unless the cache holds a pass for the current key:

- guard-paths refuses every Edit, Write, MultiEdit and NotebookEdit;
- guard-paths refuses **every Bash command** except `trimtab check-enforcement` as the whole
  command, which writes nothing. A shell can write any file, and E21 says "every file write",
  so the only Bash left while the gate fails is what reports on it. Even `git fetch` writes;
  the recorder fetches the default branch itself;
- guard-github refuses every GitHub write and every `git push`.

The current key takes three reads, on every guarded call:

- the default-branch head, by `git ls-remote https://github.com/<owner>/<name>.git` with the
  clean fetch's pinned config (so no `insteadOf` can answer with a stale head), under a 3 s
  timeout;
- the lock and the instance's base, as objects;
- the install digest, from local reads.

There is no time-to-live, so a pass is never stale. A timeout, a missing cache or a mismatch
refuses. A guard that finds no pass for the current key starts the recorder (at most one at a
time) and refuses, so a merge to the default branch mid-run costs a short wait, not a stop.

guard-github's prefilter grows to read `git push` and remote ref writes, which it now ignores.
These guards land with E21's PR; this one ships E20 and the recorder.

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
- **E12.** Counted from server facts only, so any session reaches the same count.
  - **What counts.** The part's CI-fix rounds are the heads H, pushed to any of its branches
    since its first PR opened as a draft, that meet both of these:
    - H was answered: some later push's `before` is H, in the branch-activity events (E13's
      read);
    - H has either a job record that concluded `failure`, `timed_out` or `cancelled`, or an E3
      facts run, or H **conflicted with the default branch** (§5a): the PR's `mergeable` was
      `false` for H when the answering push was made, read and recorded by `plan-push-check`.
  - **Counting.** Each such H counts once, however many jobs or attempts it has and whichever
    it answers.
  - **Excluded.**
    - The draft-opening run is not a push. Pushes that answer its review do count.
    - The E6 re-request adds no push.
    - A push whose diff from H has the E7 register-commit shape is excluded.
  - **Tighter than the row.** The round's own fix push (E10) is counted when the head it
    answers itself had a failing job. A server fact cannot tell that push from a CI fix.
  - **The limit.** `plan-push-check` refuses a push that would make the fourth, and writes a
    `stop`.
  - **The test.** "Answers a failing check" is a pure function of the activity events and job
    records, tested on recorded fixtures. E22 recomputes it on the server.
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
- **E18.** A `plan/*` push refuses when its own change does any of the things below. Its own
  change is its diff from the remote head, except for a merge of the default branch (§5a): there
  it is the merge commit's diff from the default-branch parent, so main's own changes are never
  judged as the run's.
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

### 5a. Conflicts with the default branch (owner's decision, 2026-11-12)

The project's `main` no longer requires branches to be up to date. The approver refuses only a
head that conflicts with `main`. Resolving that conflict is the run's job during the watch to
merge. The owner chose, in their words: "it counts as a CI fix round (at most 3), with one
in-session review round". So:

- **What the run does.** When the PR's `mergeable` is `false`, the run returns the PR to draft,
  merges the default branch into the `plan/*` branch, resolves the conflict and pushes.
- **It counts as a round.** That push is a CI-fix round under E12, counted against the same cap
  of three. Before the next re-request it needs one complete in-session round on the merged
  head (E10).
- **Rows that already apply:**
  - the push is a fast-forward of the branch (a merge commit on top), so E15 allows it;
  - E14, E17 and E19 apply as to any push;
  - E18 judges only the run's resolution, not main's changes;
  - E13 counts main's commits as on the default branch, not as run work.
- **Scope after the merge.** Scope (E8, `planscope`) is judged from the new merge-base, so
  main's changes are not the item's.
- **When it stops.** A conflict the run cannot resolve within the item's scope is a stop. So is a
  conflict after the third round.
- **Trimtab rule text.** This only tightens AGW-4: it counts more pushes as rounds, under the
  same cap. AGW-4 needs no edit. AGW-4's definition of a CI fix round ("answers a CI review or a
  failing check") could name a conflict explicitly; that would be a Trimtab rule edit for the
  owner.

`wrap-merged`'s watch step names this flow (its own PR).

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
  E7, E8, E9, E12, E14 and E15 through the activity events, E16, E18 on the whole diff, and
  E19.

The approver reads the job like `plan-scope` (ADR 0058): every attempt on the head passed, from
the pinned path and the `pull_request` event. Until the approver reads it, only the admin merges
a run's PR, as the doctrine record says.

## Options considered

| Option | Why it lost |
|---|---|
| Keep counts and push records on GitHub (labels, comments, commit statuses) | The agent's token can write every one of them |
| Hold the whole ledger on the server (an Actions artifact, a store the project owns) | Round entries come from the session's hooks, so the session would upload what the agent could forge; and it is new infrastructure (ENG-5) |
| Require the ledger for every act in a part (this record's first draft) | Any session lost mid-part stops the run, the watch to merge (up to 24 hours) outlives a cloud session, and every loss costs the operator a branch deletion |
| Let a ledgerless session after ready reply with the fixed or deferral tag | Each needs a push that only a session with the ledger may make |
| Count E12 in the ledger | A fresh session would restart at zero, so a hand-over would reset the cap |
| Hand a part's state between sessions in the progress record | The agent writes the progress record, so counts there are facts the agent can write |
| A ledger with an HMAC | The key would sit where the agent's shell can read it; the protection is still guard-paths, so it adds code and no fact |
| Cache the E20 pass with a time-to-live instead of keying it to the default-branch head | A merge to the default branch inside the window would run under a stale gate |
| Run the full E20 command in every hook | Dozens of API reads per write; it outlives the 6 s deadline, and a timeout fails open |
| Let `trimtab check-enforcement` write the pass (this record's first draft) | Any invocation, from a shimmed PATH or a modified copy, would write a real-looking pass; the recorder, started by the hook with the operator's environment, is the one writer |
| Accept the forged-pass risk with the command as writer | Leaves the ordinary routes open; the recorder costs one module |
| Leave Bash unconfined while the gate fails, and narrow E21 upstream | E21 says "every file write"; narrowing a row needs a Trimtab record, and until then the row would not be shown |
| Key the pass on the lock, base and head only | A hook unlinked or unregistered after a pass would leave the pass standing until the next merge |
| Detect a changed checkout with `git status` | Index flags, stat tricks and ignore rules hide edits from it; hashing every file does not |
| Read git and gh from PATH with the inherited environment | A session's PATH, `GIT_*`, `GH_HOST` and repo config would decide what the gate reads |
| Fetch into the project with the user's git config, as round 1 of this record's review had it | Verified: a session-written `insteadOf` with `ext::`, a `core.sshCommand` or a `credential.helper` runs code inside the recorder |
| Trust git's object files once replace refs and grafts are off | Verified: git does not re-hash a loose object it reads, so a substituted tree passes for modified code |
| Clear every project's pass whenever any recorder starts | Two concurrent runs on one machine would clear each other's pass forever; each recorder clears its own, and all only when it cannot name its repository |
| Count a conflict-resolving merge outside the CI-fix cap | The owner chose to count it (at most three), with one in-session round |
| Read the doctrine record and its merge through Trimtab's API | A project's agent token gets 403 on a private instance, and every run would stop; the git objects behind the admin-merged lock are a stronger fact |
| Read the lock and manifest from the working tree | The agent's shell can edit both; objects named by an admin-set SHA it cannot |
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
- Before ready, a part is tied to the machine and config dir that started it. A session lost
  mid-part before ready stops the part, which is every such loss in the cloud. Only the
  operator's branch deletion then starts part K+1. Between parts, and after ready, any session
  anywhere continues.
- More, smaller parts: each is its own PR, CI review and approver merge, so more billed reviews
  and more merges. A slow merge stalls the next part.
- Watch-only cannot fix: a check failing after ready, or a thread that needs a fix or deferral,
  stops unless a session with the ledger is still alive.
- E12 rests on the branch-activity events, so their paging and expiry are new ways to fail
  closed. Each one is a stop.
- Deferral needs a CI review whose tree equals the merge-base's. A project that wants deferrals
  to be possible needs a review of each default-branch head. Without one, every out-of-scope CI
  thread stops the run. Accepted as the strict reading.
- E10, E11 and E23 rest on the ledger only. E22 cannot recheck them server-side. A session
  that forges its ledger past guard-paths could skip in-session rounds, but it could not change
  what CI review, the E22 job or the approver see.
- **The local gate is not a fact against a session determined to forge it. Accepted, and why:**
  - The gate runs in the same account as the session. A session can run its own copy of the
    recorder, or any code, with a forged environment, or write the cache through a path that
    guard-paths cannot read from the command text.
  - Hardening closes the ordinary routes:
    - a shimmed PATH or bash, at the gate and in the hook that launches the recorder;
    - git config, global or the repository's, a partial clone's lazy fetch included;
    - gh config, which no gh call after the token read reads;
    - a CA file the session can write;
    - a `git replace`, a graft or a substituted object file, a FIFO object or a forged size;
    - an index flag or an unlistable directory;
    - a planted `__pycache__` `.pyc`, which no interpreter the hook starts reads;
    - an edit or a checkout between a check and the digest, which one snapshot rules out;
    - an edit or a checkout after a pass, which the digest catches at the next guarded call;
    - a command that happens to write the cache;
    - a recorder that dies, or one an earlier session left running.

    It cannot close the routes above: code the session runs itself, and a write through a
    hidden path.
  - **Named routes left by design:**
    - **The project's identity** is taken from its `origin` as configured, which a session can
      point at another project the instance file names. A pass is then written under that
      project's name, from that project's real server facts, and a guard looking the
      repository up the same way accepts it in this checkout. Binding a checkout to its
      repository needs operator-set state, such as an instance mapping from project paths,
      which is new configuration. E22 runs in the PR's own repository, so the borrowed pass never
      reaches a merge.
    - **Between a guarded call's snapshot and the hook running the code it read,** a session
      could swap a file: a time-of-check race of milliseconds, the same class as the residual
      above.
    - **A `.pyc` for code a guard imports** is closed only for interpreters the hook starts with
      the pycache prefix. Guards started by Claude Code's own hook runner get the same flag in
      their launch line (E21's PR).
    - **A planted extension module or sourceless `.pyc`** beside the code is loaded by Python
      before the checkout check can refuse the extra file: a `trimtab/<m>.so` wins over `m.py`.
      No interpreter flag stops this. Such a file needs a write under the code root, the same
      class as editing a file there, and the checkout check still refuses the pass.
    - **Other hooks' shebangs.** Every other hook still opens `#!/usr/bin/env bash`. That route
      exists apart from this record's work, so it gets a PR of its own off `main`.
    - **A keyring-held gh token** is read through the session's D-Bus. Where that cannot be
      reached, the operator exports `GH_TOKEN` for runs, or no pass is written (closed, not
      open).
  - What makes forgery gain nothing is E22. The server-side job and the approver re-check every
    row that rests on a server fact, and only they let a run's PR merge. A forged local pass
    buys local writes and pushes to a `plan/*` branch, never a merge.
  - A job re-run that fails after a pass is seen only at the next recorder run (the next
    session, or the next default-branch move); E22 sees it at merge.
- **Linux only, for runs.** The recorder's launch needs GNU `mv -T`, `setsid` and `timeout`
  under `/usr/bin:/bin`. macOS ships none of them there, so a plan run on macOS never gets a
  pass. That fails closed: E21's guards (their own PR) refuse every write in such a run.
  Interactive sessions are
  unaffected, and the README's platform list says so. A macOS path is not designed here.
- Every guarded call in a run costs one `git ls-remote` and a few local reads.
- While the gate fails, a run can use no Bash except `trimtab check-enforcement`.
- The instance schema grows by `enforcement`, `review`, `secret_scan` and `fix_limits`. An
  instance without them shows no project rows.

## Measurements

None. The 3 s `ls-remote` bound and the defaults are unmeasured starting values. The 60-minute
and 24-hour bounds come from the doctrine record.

## Amendment 1: guard-github's rows (E12, E14-E17, E19), second PR of four

The record above is unchanged; this amendment records what the second PR built and the choices
the rows left open. Status of the record: still proposed.

**What ships.** `hooks/guard-github.sh` now sends any command naming `push`, `send-pack`,
`http-push`, a git subcommand the shell builds, `gh pr close|merge|update-branch` or
`gh issue close` to `trimtab/adapters/guard_github.py`, which reads the spellings of a `plan/*`
write and hands them to `trimtab/planrows.py`. Each row is its own decision function
(`check_e12`, `check_e14` to `check_e17`, `check_e19`), so the mutation test removes one row at a
time. `trimtab/planfacts.py` reads GitHub and the checkout; `trimtab/planguard.py` joins the two
for the hook.

**Spellings read.**
- `git push`: options with git's abbreviation rule, refspecs (`src:dst`, `+`, `:dst`, `HEAD`,
  `refs/heads/`, `heads/`), `--delete`, `--mirror`, `--prune`, `--all`, and a bare push resolved
  through `@{push}`. A branch it cannot resolve, a refspec built by the shell or holding a
  pattern, an option it does not know, `-C` through a shell expansion, `--git-dir` and
  `--work-tree` all refuse.
- `--mirror` refuses as E15 and `--prune` as E16, whatever the refs. `--all` is judged ref by ref
  for the local `plan/*` branches.
- REST: a ref create, update (with `force`) and delete, a contents write with a `branch`, a
  merge with a `base`, a branch rename, `update-branch`, and a PATCH to `pulls/N` or `issues/N`
  with `state=closed` (the head is looked up). A GraphQL `createRef`, `createCommitOnBranch` or
  `mergeBranch` naming `plan/` refuses, as does every `closePullRequest`, because it names the PR
  by an ID this guard cannot read.
- `gh pr close`, `gh pr merge --delete-branch`, `gh pr update-branch` and `gh issue close`, by
  number, URL, branch or the current branch.
- Branch names are compared case-folded and looked up as spelled.

**Choices.**
- **E12 counts the run, not each job.** A head fails when a run for the branch concluded
  `failure`, `timed_out` or `cancelled` on its latest attempt. One list read replaces a job read
  per head (ENG-6.2); a failure re-run to success reads as passed, which is AGW-4's "an
  infrastructure failure is re-run once and does not count". An E3 facts run is a run of the
  instance's `review.facts` workflow. With no `review` key E12 cannot be counted and refuses.
- **The conflict count has a gap until the ledger.** A head that conflicted with the default
  branch counts when `mergeable` is `false` for the head the push answers, read at that push. The
  record of past conflicts is the ledger's (the plan-run layer's PR); until it lands, an earlier
  conflict-only round is not counted, and E22 has no server fact for it either. The cap is
  therefore weaker by conflict-only rounds until then.
- **E19 is stateless.** Each guarded write re-reads the scan job's records for the branch's
  latest head, so a failing head keeps the stop standing without a stored stop. Writing a `stop`
  to the ledger is the plan-run layer's. No `secret_scan` key refuses.
- **The instance schema gains `secret_scan` and `review`** (the job, the review's login, the
  re-request label, the five tags and the two bounds, as §4 lists them). `fix_limits` is E18's,
  in the next PR.
- **Hook-path imports are standard library only.** `GhApi`, its errors and `git_bytes` moved to
  `trimtab/capture/ghapi.py`, re-exported from `capture/sources.py`. The hook runs Python with
  `-I -S`, where PyYAML (reached through `capture/sources.py`) is not found; so did the recorder
  under `-I` on a host that installs PyYAML for the user only, which left the recorder without a
  pass. Both now import without it.

**Residuals accepted.**
- A git alias (`git -c alias.x=push x`) or a push through a binary the guard does not name is not
  read; the push still faces E22 at merge.
- The hook's 10 s limit holds the adapter to 6 s for several GitHub reads. A slow read refuses
  the push (closed), and the run retries.
- A bare push in a shallow clone refuses, because the gate's git refuses shallow history.
- `update-branch`, a contents write and a ref update by REST carry no source commit, so on a ready
  PR they refuse (E17) and cannot take the register-commit exception.

**Review round 1 (three passes) changed these, and added these residuals.**
- **Read as well:**
  - A command word the shell builds, followed by a push or another word the shell builds, when the
    command names `push` or `plan/`.
  - An HTTP method the shell builds.
  - A PR or issue number the shell builds in a close, merge or `update-branch` route.
  - A GraphQL `updatePullRequest` with a `state`, every `updatePullRequestBranch`, and string
    escapes in a ref or commit mutation.
  - A merge into a `plan/*` base, by `gh pr merge` or `PUT pulls/N/merge` (the PR's base is looked
    up, so every merge now needs that read; an unreadable base refuses).
- **Routes are anchored to `repos/<owner>/<name>/`** and contents is matched before refs, so a
  contents path that looks like a ref route is still contents. A ref write to another repository
  than the facts are read for is refused, after the rows that read nothing (E14, E15, E16).
- **Git configuration that steers a push.** `-c` keys under `remote.`, `push.`, `url.`, `alias.`,
  `include`, `branch.` and `core.sshCommand`, `--config-env`, and the variables `GIT_CONFIG_*`,
  `GIT_DIR` and kin on the command refuse. The repository's effective configuration is read for
  every push: a mirror remote refuses (E15), a bare push with `remote.<r>.push` refuses, and
  `push.default=matching` refuses when local `plan/*` branches exist.
- **Where a push goes.** For a push that writes `plan/*`, the destination is the named remote's
  `pushurl`, else its `url`, or the literal URL; it must be the repository the facts are read for.
  `url.<base>.insteadOf` refuses. Facts, diffs and local branches come from the directory the push
  runs in (`-C`).
- **`--all` carries `-f`, `--force-with-lease` and `--delete` into each local `plan/*` branch.** A
  first word the shell builds, before a `plan/*` refspec, may be an option and refuses.
- **The prefilter** also passes a brace expansion that can spell `git` or `push`, and the
  `git-push` binary.
- **E12's cap.** The code holds the owner's three rounds (§5a). AGW-4's text says two; changing
  the rule is the owner's, in a Trimtab PR of its own.

Residuals added:
- A command built from variables defined outside the command (`$G $S origin x` where `G` and `S`
  came from the environment) is not seen by the prefilter and is not read.
- Configuration set by an earlier command (`git config remote.origin.push …`) is read when the push
  runs, not when it is set; a push is then refused if the effective setting steers it.
- A sourceless write (`update-branch`, a contents write, a ref update with no sha) on a draft whose
  head is failing or conflicted refuses, as it does on a ready PR, because the register-commit shape
  cannot be shown for it.

**Review round 2 (three passes) changed these, and added these residuals.**
- **Read as well:**
  - A REST route that ends in `graphql` is REST; only a bare `graphql` path is GraphQL.
  - A directory change (`cd`, `pushd`, `popd`, `env -C`, `--chdir`) or a variable that steers git
    (`GIT_*`, `HOME`, `XDG_CONFIG_HOME`) anywhere in the command text, including nested strings and
    earlier segments. A push after such a change is read only if it names no plan branch.
  - A command that assembles `git` and its subcommand from variables inside the command.
  - A write to a ref, contents, merge, rename, PR-update or PR-close route whose path carries a
    query string.
  - `gh pr close`, `merge` and `update-branch` with no selector take the PR from the checked-out
    branch's upstream (`branch.<b>.merge`), as `gh` does.
  - A remote word the shell builds (it can split into options or refspecs of its own), a refspec with
    no `:dst` while `remote.<r>.push` maps it, `-c alias.*`, GraphQL `mergePullRequest` and
    `enablePullRequestAutoMerge`.
- **Configuration is read as a push sees it:** global and system configuration are read for the
  steering keys, though never for objects. The gate's own reads still ignore them.
- **A 404 is absence only where the repository answers.** A branch or PR that returns 404 while the
  repository itself is unreadable is a refusal. Every other 404 is a `FactsError`. A PR is read once
  for its head and base.
- **A push after a `cd` that names no plan branch is no longer refused** (round 1 over-refused it).

Residuals added:
- **Alias residual stays.** A stored alias (`alias.p=push` in a config file) used as `git p …` never
  reaches the prefilter; only `-c alias.*` on the command is read. E22 is the bound.
- **Commands that run a string**, such as `git rebase -x`, `git submodule foreach`,
  `git bisect run` and `git filter-branch --exec`, and a renamed copy of the git binary, are not read.
- **`--all` reads facts once per local `plan/*` branch.** Each is a handful of `gh` calls against
  the adapter's 6 s limit. No timing has been measured; a slow read refuses.
- **Not done in this PR** (medium and low findings left for the owner's decision): refusal text still
  quotes words from the command in a few places, `cancelled` runs count as E12 rounds, activity
  paging may need cursors, a binary change to the progress record can pass as register-only, E19 reads
  only the latest head, selector URLs are matched case-sensitively, and `merge-upstream` of a
  `plan/*` branch is not read.

**Triage after round 2 (4e0e8d3), and review round 3's HIGH findings.** Earlier rounds above are
unchanged; this records what changed since and supersedes their "Not done in this PR" list.

Changed in 4e0e8d3:
- REST branch fields are normalised (`refs/heads/`, `heads/`, padding) before the `plan/` test.
- A binary change to the progress record is neither register-only nor progress-only.
- A configuration read needs none of the object checks, so a shallow, partial or alternates clone
  pushes like any other (round 3 removed the check; 4e0e8d3 had kept it).
- Git booleans are read as git reads them, integers included.
- `gh` selectors: `GH_REPO` or `GH_HOST` in the command, a repository option in the attached forms,
  a number selected after a directory change, and a URL the guard cannot read all refuse.
- A scan workflow that never runs on a branch that has runs of others reads "unreadable", so a typo
  cannot switch E19 off. E19 still reads the latest head, as the row says.
- Refusal text names the kind of problem, not words from the command or `gh`'s stderr.
- GraphQL `updatePullRequest` refuses for a `state` argument or variable, not the word in a body.
- Branch activity is one page of 100 events, and a full page refuses: the endpoint pages by a cursor
  in a Link header (`after=`), which `gh api` does not return, and ignores `page=`.

Changed for round 3's HIGH findings:
- **Prefilter:** a backslash before a newline inside a word, a brace sequence (`{a..b}`), and a
  command that assigns variables and expands them all reach the adapter.
- **Dynamic command word:** only the command word is judged, so `git add src/*.py` and
  `git diff "$A" "$B"` pass again (round 2 refused them). A command word built from variables in a
  command that assigns variables refuses.
- **State changed earlier in the same command.** A name change (`switch`, `checkout`, `branch`), a
  content change (`commit`, `merge`, `reset`, `rebase`) and a configuration change (`config`,
  `remote`) are told apart. A push that names a plan branch after a name or content change, a bare or
  `HEAD` push after a name change, and a bare or destination-less push after a configuration change all
  refuse. A plan run therefore commits and pushes in separate commands. An ordinary
  `git commit … && git push origin feature/x` passes.
- **`push.default=upstream`** maps a refspec with no `:dst` onto the branch's upstream; it refuses
  when any branch's upstream is a `plan/*` branch.
- **`git subtree`, `git-subtree` and a `{}` refspec** (`find -exec`) are not read and refuse. GraphQL
  `createLinkedBranch` is judged like `createRef`.
- **gh placeholders** (`{owner}`, `{repo}`, `{branch}`) on a ref, contents, merge or PR route refuse.
- **`gh pr update-branch` and `merge`** carry the repository of `-R` or the URL to the write, so it is
  judged against that repository's facts or refused; a branch selected in another repository refuses.
  `gh repo set-default` (a `remote.*.gh-resolved` setting) refuses, because gh then acts on a
  repository that may not be origin's.
- **`git -C ~/x`** refuses: the guard does not expand a tilde.

Left on purpose, still bounded by E22: the cost of the prefilter's new alternatives is unmeasured
beyond the guard suite's long-command cases; `cancelled` runs count as E12 rounds; selector forms
`http://…` refuse rather than parse; the unmeasured 6 s budget; a push from a repository reached by
`cd` is judged without that repository's configuration; a gh alias, a legacy `.git/remotes` push
line, and a command assembled wholly from variables that the command does not itself assign.

**claude-review on the ready PR (comment on PR 166).**
- **Prefilter, brace groups.** `g{i,}t pu{s,}h origin HEAD:plan/x` names neither `git` nor `push` whole and
  none of the earlier brace patterns matched it. Any brace group with a comma, or a `..` sequence, now
  reaches the adapter, which reads a brace group as a word the shell builds. The braces of the payload's
  own JSON have other braces or no comma between them, so they do not match.
- **E19 allows a push while the scan is `pending`.** This is the row's own text ("pending allows writes but
  refuses ready"), so a push made before the scan finishes can introduce a secret that the scan then
  fails; the next guarded write is refused, and ready is refused. The server-side E22 job and the
  operator's own scanner merge gate bound it. It is a residual, not a gap in the row.
- **Times are parsed, not compared as text.** Branch-activity times and the PR's opening are RFC 3339
  with their offset; a time that cannot be parsed, or has no offset, refuses.

**claude-review on 9e10bd5.** A push built only from command substitutions (`$(printf gi)t pu$(echo s)h
origin HEAD`) named no `git`, `push` or `gh` and held no brace group, so the prefilter exited 0. Any
command holding a substitution (`$(` or a backquote) now reaches the adapter, which refuses a command word
assembled from a substitution and other characters (`$(printf gi)t`, `g$(echo i)t`). A substitution followed
by `/`, `.` or `-` (`$(dirname "$0")/x.sh`) is a path and passes, as does a substitution in an argument. The
cost is that a command with a substitution starts python3 (about 50 ms), and a command word that is a
substitution plus letters is refused. Still not read: a push whose every word is a bare substitution
(`$(echo git) $(echo push) origin x`) and a substitution run through a shell string, which the first
"state and strings" residual already names; E22 bounds both.

## Amendment 2: the ledger and the pure review rows, 2a of the plan-run layer

Amendment 1 is unchanged. The plan-run layer (this record's third PR) lands in three parts, 2a, 2b and
2c, at the owner's choice of 2026-11-13; each cites this record. This amendment is appended as each part
lands, and records what that part built and chose.

**2a ships** the pieces with no caller yet, so each is tested on its own and nothing reads them until 2b:
- **The ledger store** (`trimtab/planledger.py`, `guardstate.append`): the §1 path, six event types
  (`push`, `round`, `draft`, `rerequest`, `stop`, `conflict`), a version and an injected clock on every
  line. A file is opened without following a link, must be private and regular, is locked for the append
  and is bounded at `guardstate.MAX_BYTES`. A malformed line, an unknown version or type is an error, never
  skipped. `start` refuses when the part's branch is already on the remote and leaves an existing ledger as
  it is. A repeated pass on a head, `stop` reason, `draft` or `conflict` is written once (REL-1).
- **Cursor paging** (`GhApi.call_with_headers`, `GhApi.cursor_pages`): `gh api -i` and the `after=` cursor
  of the Link header's `rel="next"`. `page=` is never sent. The page limit reached with a next link still
  standing is an error, as is a next link with no cursor. E13 reads the activity events through it in 2b.
- **`fix_limits`** in `instance.json` (E18): `markers` and `thresholds`, inline lists. Absent means no E18
  rows are shown. Present with no `markers`, it takes `instance.DEFAULT_FIX_MARKERS`; `thresholds` are
  patterns that must compile and have no default.
- **The pure rows** (`trimtab/planreview.py`): `check_e5`, `check_e6`, `check_e7`, `check_e8`, `check_e9`,
  `check_e10`, `check_e13`, `check_e18`, `check_e23` and `check_round_start`, one decision function each;
  `parse_facts` for the E3 lines; `thread_key`; `next_part` for §6. A fact that could not be read is the
  caller's `FactsError`.
- **The E3 parser** reads only lines that begin with a facts marker, after an Actions timestamp. A marker
  inside a longer line, such as a model's output echoed into the log, is not read.

**Choices.**
- **`fix_limits.markers` is inline**, not "a file of literal strings" (E18's text). Reading a file as an
  object at `trimtab_sha` needs a second read for no gain: the list is the project's, merged by its admin.
  Rejected: a path to a file in the instance repository. It can move to a file later without loosening.
- **E9 also refuses a fixed reply whose thread is not yet closed** by a later E3 line from someone other than
  the agent. The row says a thread "counts as closed only when" so; the check reads that as ready needing
  it. Tighter than a looser reading, and E22 sees the same facts.
- **E13's ledger-to-events agreement is exact:** the ordered `(before, after)` pairs must equal the ledger's
  `(old, new)` pairs. A push the ledger did not see (a merge made on GitHub) breaks it, which is a stop.
- **E18 judges added and removed line text**, so `planrows.Change` gains `removed_lines`. A removed
  threshold line the plan's item section does not name refuses even where nothing replaces it.
- **The rows are in `planreview.py`, not `planrows.py`**, so the write rows keep their size and `planrows`
  stays the one module with the push reader. It imports `Change`, `FactsError` and `Refusal` from it.

**Not in 2a (so the manifest has no entries for these rows yet).** A row is "shown" only with a hook that
calls it, so E5-E11, E13, E18, E21 and E23 get their manifest entries in 2b, with the subcommands
(`plan-ready-check`, `plan-rerequest-check`, `plan-push-check`, `plan-resume-check`), the facts readers and
the guard wiring. 2c holds E22 and its lock field. The mutation check for 2a's rows was run by hand: with
each decision function replaced by one that allows, at least one of its tests fails.

**Review round 1 (three passes) changed these in 2a.**
- **The E3 parser splits the log on `\n` only** (`str.splitlines()` also splits on U+2028, `\x0b`, `\x0c`,
  `\x85` and a bare `\r`, which let a repository filename start a forged `resolved` line) and accepts a
  `path` of `[A-Za-z0-9._/@+-]` only. A path that breaks either way fails the whole read.
- **E18 pairs threshold lines by text with the numbers masked**, so an unrelated added line cannot stand in
  for a removed one; a removed line with no replacement refuses; a replacement the plan names exactly may be
  lower. Markers match without regard to case or spacing, the defaults are wider (`xfail`, `pytest.skip(`,
  `if: false`, `--no-verify`, `# pyright: ignore`), and `fix_limits.markers: []` is refused rather than
  switching the check off. Negative numbers are read as negative.
- **E7 takes the latest verdict per head**, as E6 does. **E10** refuses when a ledger push is not on the
  branch rather than leaving it uncounted. **E23** takes the heads, not a count. **E9** refuses a thread with
  no anchor commit.
- **The ledger** checks every directory level on read and append, requires each line's type fields, and
  `guardstate.append` takes the lock before it reads the size. **Link headers** are read whole (repeated
  headers joined, `rel` anywhere in an entry); a header that says `next` in a form this reader cannot place is
  an error, as is a page limit below 1 or a caller-set `page`, `after` or `per_page`.

**Residuals recorded here (not fixed in PR 166 or 2a); E22 bounds the shell-spelling ones.**
- A `fix_limits.thresholds` pattern is the admin's and is only compiled; a catastrophic pattern would stall
  the hook, which fails closed at the 6 s deadline. `Thread.commit` is not in the E3 line, so 2b fills it
  from the review comment before E9 can pass. The `once` de-duplication reads outside the lock, so two
  hooks at once can write one line twice; readers treat that as one.
- The facts job is a separate job with no model step (§4), which is what keeps model output out of the
  log's line starts; the parser's own checks above are a second layer, not a nonce binding.
- A push spelled with a glob (`git pu?h` with a file named `push` in the directory), a push whose every
  word is a bare substitution, a stored git alias, commands that run a string (`git rebase -x`,
  `git submodule foreach`, `git bisect run`, `git filter-branch --exec`) and a legacy `.git/remotes` push
  line are not read.
- PR 166's third review round, medium and low: a bare push on a new branch under `push.default=current`;
  `gh issue close <URL>` refused; GraphQL close and merge refused for every PR; a refusal text that names the
  `-C` directory; two weak tests; the unused `before` parameter in `check_git`; unused imports in
  `capture/sources.py`.
- A `cancelled` run counts as an E12 round, and the 6 s hook budget is unmeasured.
- The ledger, E10, E11 and E23 rest on a file the session's account can write; the ADR 0028 residual stands.
