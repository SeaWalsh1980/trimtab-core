# The gate on the next work

The conditions of plan-run/SKILL.md section 4. Every one must hold.

- It names an item that exists in the authorised plan (as `check-plan-authorised --print-plan`
  printed it) and is not already complete. Anything else is a blocker — a hand-over cannot create
  work.
- It is not `plan complete`. A `blocked: <reason>` Next is a stop, except in a valid restart
  ("Restarting after a stop"), where the next work comes from the restart rule below instead. That
  rule is plan-run/references/restart.md.
- Doing it would stay inside the plan's scope: every path it writes is on the write allowlist
  **and** matches a line of `<plan>.writes` (as `check-plan-authorised --print-writes` printed it;
  same glob syntax, `+` for create-only), or lies in the plan's own `<plans>/progress/<plan>/` or
  `<plans>/unauthorised/<plan>/` folder. Those two folders are always create-only, and any other
  path is create-only when either list says so. A change outside the scope is not made: it becomes a
  register entry, or a blocker when the item needs it.
- Doing it would not need a write to any always-refused path. Plan approval never covers these:
  - the instruction file, `CLAUDE.md`;
  - every security control: CI workflows, `CODEOWNERS`, Claude Code settings and hooks, scanner
    exemption lists, repository protection, Actions configuration, repository access;
  - the harness: the rules directory (or the lock's `rules_dir`, with its `overrides.md`), the
    Trimtab lock, an instance's `instance.json`, agent, command and skill definitions;
  - `<plans>/authorised/`.
