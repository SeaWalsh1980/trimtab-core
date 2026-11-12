---
name: plan-item
description: Work one item, or one part of an item, of an authorised plan in one PR — the `plan/…` branch and draft, the progress record and register entries, the plan's write scope, scratch files, labels, and when to stop. Use once plan-run has gated the next work, or when you start or open the PR for a plan item; not for deciding what comes next (plan-run) or for review rounds, ready and merge (plan-pr).
---

# Work a plan item

Paths such as `plan-run/references/stops.md` are relative to the skills directory, the parent of
this skill's own directory.

`<plans>` and `<plan>` are as plan-run/SKILL.md defines them. Section numbers below are
plan-run/SKILL.md's.

## Working a plan item

One item, or one part of an item, per session, ending in one PR:

1. **Open a draft PR when the work starts**, on a branch named `plan/<plan>/item-NN-partK` (so a
   later session can see it is in flight), and push a checkpoint at each milestone. If the session
   dies, the next one resumes from the branch instead of starting over.
2. **Before requesting ready**, write the progress record and any register entries: see
   plan-item/references/records.md.

   Write only inside the plan's scope (section 4's first scope bullet). When the guard refuses a
   write as outside the plan's scope, do not look for another route: record the change as an entry,
   or stop (step 5) if the item needs it. Keep temporary files only in the session scratch
   directory: the one the session-scratch hook named at session start, or the harness's scratchpad;
   the hook removes both when the session ends (trimtab-core ADR 0049). Where neither exists, make
   one directory under the system temp directory named after the session and remove it before the
   session ends. Name in `## Done` anything you leave that the hook does not remove. Section 4's
   scope bullet is in plan-run/references/gate.md.
3. **Label** the PR `plan:continue` unless `## Next` is `plan complete` or `blocked: <reason>`.
   (When you author a PR that adds an authorised plan, label it `plan:continue` too: its merge
   starts item 01 part 1. Put it on a branch outside `plan/`: `plan/*` branches are for item PRs
   only, and any other `plan/…` branch has every write refused.)
4. Review in session and mark the PR ready: see plan-pr/SKILL.md.
5. **Stop when** the work cannot be finished, needs work outside its scope, needs a write a hook
   refuses, needs the authorised plan changed, ends its third review round with a CRITICAL or HIGH
   finding, finds a CRITICAL or HIGH outside its scope in any round, exhausts the CI fix-round
   limit, or you can already see the next work would fail the gate in section 4. Then stop as
   plan-run/references/stops.md says.

The session ends once the PR is marked ready and any CI fix rounds it can act on are done, or it has
stopped. The PR's merge starts the session after.
