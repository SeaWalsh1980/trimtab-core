---
description: Plan a change with the harness items that govern its files loaded, in a planner's own context, and return an ID-cited plan as a proposed todo list whose `## Harness items applied` block goes into the PR body unchanged.
argument-hint: "[what to build or change, plus any paths you already know]"
---

Plan this change: `$ARGUMENTS`

## Why this exists

Path-scoped rules load only when a matching file is *read*, so a plan written
from memory is written without them, and a plan that misses a rule produces a
change that breaks it. This command puts planning in a subagent that resolves
the governing items deterministically (`trimtab rules-for`), reads them in
full, and cites them **by ID** — so the plan is checkable against the rules by
the reviewer and by CI, and the main session never loads the corpus.

## What to run

1. **If the requirement itself is unclear**, brainstorm it first and come back
   with the outcome. The planner plans a change; it does not discover what the
   change should be.
2. **Find the tools and the planner.**

   ```bash
   TRIMTAB="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bin/trimtab"
   "$TRIMTAB" --help >/dev/null && echo ok
   ```

   Read `.claude/trimtab.json`: its `planner_agent` names the agent to spawn
   (default `trimtab-planner`). If the file is absent, the project has not
   adopted Trimtab; plan with `trimtab-planner` against the base rules and say
   so.
3. **Spawn the planner** with: the task as given, every path already known, and
   the base branch (the default branch unless told otherwise). Tell it
   explicitly that it inherits neither the instruction file nor the doctrine and
   must load them, and that the resolver is `"$TRIMTAB" rules-for`. Wait for
   it. **Note the tokens and duration the Agent result reports**: they are the
   plan's cost in `## Process cost` (best-effort, spike S3).
4. **Check the plan before presenting it.**
   - Its `Rules loaded` section contains the resolver's output.
   - Its `## Harness items applied` holds one yaml block that parses. Save that
     section to a scratch file and run:

     ```bash
     "$TRIMTAB" citations --plan-file <scratch file> --files <the plan's footprint>
     ```

     `unknown` must be 0. Every `uncited binding` ID must either be cited or
     explained in the plan as not binding.
   - If either check fails, or the planner reports that it stopped (a resolver
     error, an unreadable rule file, a path it could not place), **do not
     present it as a plan**: report the stop, fix the cause, and run it again.
5. **Present the plan unchanged**: footprint, rules loaded, items applied,
   steps with their IDs, conflicts, harness feedback, open questions. Do not
   trim the citations: they are what the reviewer checks.
6. **Stop.** The plan is a proposed todo list, not an execution. Request explicit
   authorisation before executing any item, and answer the open questions first.

## After authorisation

Executing an item makes the same rule files load when their files are edited.
Keep the plan's `## Harness items applied` and `## Harness feedback` blocks:

- Pass the first to `/trimtab-review`, which checks every citation against the
  diff.
- **Put all three sections in the PR body** (`## Harness items applied`,
  `## Harness feedback`, `## Process cost`; see the project's PR template), with
  the planner's cost under `plan:` and the review's passes under `review:`.
  Check the body before opening the PR, as a draft:

  ```bash
  "$TRIMTAB" check-pr --body-file <body file>
  ```

  CI's PR-body job is the real gate; the local hook and this check only catch it
  earlier.
