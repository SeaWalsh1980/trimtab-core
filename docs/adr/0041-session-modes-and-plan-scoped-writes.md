# 0041. Session modes, and plan-scoped writes for autonomous runs

## Status

Accepted on merge. Amends ADR 0039
(`docs/adr/0039-guard-paths-refuses-the-harness-and-enforces-a-write-allowlist.md`)
and ADR 0040 (`docs/adr/0040-write-allowlist-create-only-entries.md`) by
adding a third layer to guard-paths and a session-mode classifier; neither
is edited. This changes security controls (`hooks/guard-paths.sh`, the new
`hooks/session-mode.sh`, and their registration in `settings.base.json`);
the operator authorised it in chat.

## Context

There are two kinds of session, and this is the concept everything below
serves:

- **Interactive:** an admin or user is in the loop and confirms as they go.
- **Autonomous plan run:** automation with the person taken out of the
  loop. The person's part comes first. The admin confirms what the plan is
  and what its goal is by merging it under the authorised plans directory.
  The plan then carries strict conditions on what the run may do.

**A run must never be able to do more than an interactive session could. It
is always the narrower of the two.**

Before this change the write allowlist (ADR 0039) was the same for every
session. A run could write anything on it, a README for instance, whether
or not its plan asked for that. And nothing told a session which kind it
was, so nothing could hold a run to the narrower rules.

## Decision

**Every session has a mode, decided by one classifier.**
`hooks/session-mode.sh` decides `interactive` or `autonomous`, failing toward
strict. A run mistaken for an interactive session would get the looser rules,
which is the one failure the concept forbids. A session is **autonomous when
any one** of these holds:

1. `TRIMTAB_PLAN_RUN=1` is in the hook process's environment. The operator
   sets it in a cloud environment or exports it before a local run. An inline
   prefix on a Bash command never reaches a hook, so the agent cannot set it.
2. The repository is on a `plan/*` branch. Plan work happens only there, so
   this catches a run whose variable was forgotten.
3. The session was recorded as autonomous earlier. So the mode only
   tightens, never loosens, within a session.
   - **Where:** `<config dir>/guard-state/session-mode/<sha256 of session_id>`,
     written only by hooks. guard-paths refuses every write to `guard-state/`
     by a tool. Reads are allowed, and the records are empty.
   - **A clear starts a new session id,** whether `/clear` or a parent
     clearing its worker, so the record does not survive it. That's why
     every autonomous run, local as well as cloud, must be launched with
     `TRIMTAB_PLAN_RUN=1`: no clear removes it. A run relying on signals 2
     and 3 alone is interactive again after a clear, once it is off its plan
     branch.
   - **Shared with pr_monitor (ADR 0028):** the plain-token session id, the
     hashed name, and the two-day TTL. A run refreshes its record on every
     write. Records past the TTL, and any planted link, are pruned once per
     session, when its record is created.
   - **A link** at the record's path reads as recorded.

A session is interactive only when none holds. The classifier is both a
SessionStart hook and a library that guard-paths sources:

- **As a hook,** it announces the mode, and the signals that decided it, in
  the session's context, so the session works by its mode from the first
  turn.
- **As a library,** it is what guard-paths enforces. The mode a session is
  told and the mode enforced cannot disagree.

**Layer 3: plan scope.** In an autonomous run a write must pass layers 1 and
2 as before, and must also be in the plan's scope:

- **The plan:** named by the branch the rules give its PRs,
  `plan/<plan>/item-NN-partK`. Any other branch refuses every write.
- **The scope:** the plan's `<plan>.writes` declaration (one glob per line, in
  the allowlist's syntax, `+` meaning create-only), plus that plan's own
  progress-record and issue-register folders. Both the plan and its
  declaration are read from the merge-base tree, which holds what the admin
  merged; layer 1 already refuses either to every write.
- **Create-only:** if either the allowlist or the scope matches create-only,
  the path is create-only, and ADR 0040's new-on-this-branch check decides.
- **Fail closed:** a missing classifier, a plan or declaration that cannot
  be read at the merge-base, no `origin/HEAD`, or a run in a repository with
  no allowlist all refuse every write.

`HOOK_ALLOW_HARNESS=1` lifts layer 3 with layers 1 and 2, as before.

**The register becomes one file per entry,**
`<plans>/unauthorised/<plan>/item-NN-partK-MM.md`, under create-only scope.
Once an entry merges, no run can change it, so changing an entry's status is
left to the operator.

**Cost.** Per write in a repository, the hook runs:
- **every session:** one `git symbolic-ref` to classify, plus one hash of
  the session id when the payload carries one;
- **a plan run:** four more `git` calls (`origin/HEAD`, merge-base,
  `cat-file`, `show`);
- **a create-only change in a plan run:** one more (`ls-tree`). The base is
  memoised, so it is looked up once per write (ENG-6.2).

Measured on a workstation, 40 calls each, for an Edit in a repository with
an allowlist, after a warm-up round:

| Case | ms per call |
|---|---|
| main before this change | 11 |
| this change, interactive, on a host with no record directory (hashing skipped) | 15–16 |
| this change, interactive, record directory present | 19–20 |
| this change, plan run | 32–35 |

All are far inside the hook's 10 s timeout.

## Options considered

| Option | Why it lost |
|---|---|
| One allowlist for every session, as before | A run could write anything the allowlist lets a person's session write; the plan's conditions were text, not a control (SEC-4) |
| Name the plan in the environment (`TRIMTAB_PLAN=<plan>`) | A cloud environment's variables are set once and shared by every session the routine starts. The next plan's sessions would read the previous plan's name, and two plans could not run at once. The branch names the plan per session already |
| Detect a run only by the environment variable | A run launched without it would be treated as interactive and get the looser rules |
| Detect the mode per call, with no record | Leaving the plan branch would turn a run back into an interactive session for the rest of its life. Rejected by the operator in favour of a sticky record |
| Keep the scope inside the plan's markdown | Parsing prose in a guard is fragile. A separate declaration in the allowlist's syntax reuses the same matcher |
| One register file per plan, appended to | Forces a plain allowlist entry, so a run can rewrite an entry's status, which the rules forbid but nothing enforced |
| Confine shell writes in the session (parse commands, or an OS sandbox) | The commands that write can't be enumerated, and a sandbox needs per-project tuning. The gate for shell writes and forged local git state is a server-side check of the PR's diff, a separate change |

## Consequences

- An autonomous run writes only what its admin-merged plan declares, plus
  its own bookkeeping, and never more than the allowlist allows.
  Everything else is refused at the write. The run records it as an
  issue-register entry, or stops as a blocker when the item needs it.
- Every write in a repository now runs one more `git` call to classify the
  session, and a plan run a few more. The cost is above.
- **The sticky record locks the whole session.** Any write in a repository
  on a `plan/*` branch records the session as autonomous, even when it's a
  person working interactively, for example to fix a run's CI.
  - From then on, for the rest of that session, every write in every
    repository is held to the run rules. On a branch that isn't
    `plan/<plan>/item-NN-partK`, that means every write is refused.
  - Ways out: start a new session, or relaunch with `HOOK_ALLOW_HARNESS=1`.
    A record left idle expires after the two-day TTL.
  - This is the price of a mode that only tightens: no signal inside the
    session can tell a person who checked out a plan branch from a run that
    wants to leave one.
- A session whose payload carries no usable session id gets no record. It
  is still classified by the other signals on every call.
- A missing classifier leaves the guards installed, but guard-paths refuses
  every write inside a repository. The SessionStart liveness check reports
  this as SESSION MODE MISSING, not GUARDS OFF, so the warning says what
  actually happens. bootstrap probes the classifier through the symlink and
  fails an install whose classifier announces no mode.
- The record directory holds one empty file per autonomous session that has
  been active within the TTL. A host with neither `sha256sum` nor `shasum`
  can't name a record, so its sessions keep signals 1 and 2 but lose
  stickiness.
- These residuals remain:
  - shell writes (ADR 0039);
  - forged local git state, such as the branch or `origin/HEAD` (ADR 0040);
  - a shell route to the record through a path built at run time.

  The worst case of a forged branch is another admin-authorised plan's scope.
  A server-side plan-scope check on the PR's diff is the gate for all three,
  with code-owner review at merge until it is required.
- A cloud-session marker, as a fourth signal, waits on the cloud spike.
