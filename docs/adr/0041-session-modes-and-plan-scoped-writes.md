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
3. The session was recorded as autonomous earlier. The record lives at
   `<config dir>/guard-state/session-mode/<session_id>`, written only by
   hooks; guard-paths refuses `guard-state/` to every tool. So the mode only
   tightens, never loosens, within a session. A link at the record's path
   reads as recorded.

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

**Cost, measured** on a workstation, 40 calls each, for an Edit in a
repository with an allowlist:
- main before this change: 11 ms per call;
- this change, interactive: 14 ms;
- this change, plan run: 23 ms. The extra is a `git symbolic-ref`, a
  merge-base, a `cat-file` and a `show`.

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
  session. A plan run runs about four. The measurements are above.
- A person working interactively on a `plan/*` branch gets the strict rules
  and keeps them for the session. Lifting them needs `HOOK_ALLOW_HARNESS=1`.
- A session whose payload carries no usable session id gets no record. It
  is still classified by the other signals on every call.
- The record directory grows by one empty file per autonomous session. It is
  not pruned yet.
- These residuals remain:
  - shell writes (ADR 0039);
  - forged local git state, such as the branch or `origin/HEAD` (ADR 0040);
  - a shell route to the record through a path built at run time.

  The worst case of a forged branch is another admin-authorised plan's scope.
  A server-side plan-scope check on the PR's diff is the gate for all three,
  with code-owner review at merge until it is required.
- A cloud-session marker, as a fourth signal, waits on the cloud spike.
