# 0061. guard-paths refuses a `.claude` directory at any depth

## Status

Accepted on merge. Extends layer 1 of ADR 0039 (the always-refused harness paths), which this
record does not rewrite. It is a change to a security control (SEC-7), authorised in chat for
this change.

## Context

Layer 1 refuses `.claude/` at the repository root and `CLAUDE.md` and `CLAUDE.local.md` at any
depth. It is implemented twice and the two must agree: `hl_refused` in `hooks/guard-paths.sh`
for the session, and `refused()` in `trimtab/planscope.py` for the `plan-scope` check (ADR 0042),
which merge-authority also reads for its admin-only paths (ADR 0057).

A trial of a consumer's plan-merge approver found that plan-scope refused a nested `CLAUDE.md`
in an item PR but passed a nested `.claude/rules/`, `.claude/skills/` and `.claude/agents/`.
Any path under a directory the write allowlist and the plan's declaration cover reaches the
lists, since `*` spans directories. ADR 0039 did not consider a nested `.claude/`.

A nested `.claude/` is harness, as a nested `CLAUDE.md` is. Claude Code loads a subdirectory's
`.claude/skills/` and `.claude/rules/` once it works on files there, and a session launched in
that subdirectory reads its `.claude/settings.json`, hooks included, as project settings. Its
documentation does not say whether nested agents and commands below the launch directory are
read. A plan run that can write one can plant instructions or hooks that a later session loads.

The consumer in the trial covers `.claude/**` in CODEOWNERS, so the owner's review still caught
it at merge. Not every consumer does, and plan-scope is the generic check.

## Decision

Layer 1 also refuses any path with a `.claude` segment below the root: `*/.claude` and
`*/.claude/*`, compared lower-cased like the rest of the layer. The match is on the whole
segment, so `.claudex/`, `x.claude` and `.claude.json` stay ordinary paths. A file named
`.claude` is refused as a directory is, as it already is at the root.

The check runs before the root `.claude/worktrees/` exemption. A real worktree has its own
`.git` and is judged as its own root, so its paths never reach the check with that prefix; a
stale worktree directory without `.git` now has its nested `.claude/` refused, which fails
closed.

## Options rejected

| Option | Why it lost |
|---|---|
| Refuse only nested `.claude/rules/`, `.claude/skills/` and `.claude/agents/` | Misses nested settings, hooks and commands, which are harness too; three shapes to keep in parity between bash and Python instead of one |
| Leave a nested `.claude/` to CODEOWNERS at merge | Coverage then depends on each consumer's CODEOWNERS; plan-scope is the generic check every consumer gets, and CODEOWNERS stays the backstop rather than the only gate |
| Exempt a file literally named `.claude` | A second shape to keep in parity, for a file no repository needs |

## Consequences

- A repository that tracks a nested `.claude/` needs the operator's `HOOK_ALLOW_HARNESS=1` to
  edit it in a session, as for the root one. Neither the base nor the instance tracks one.
- A Bash command naming a nested `.claude` path is refused, reads included: the trade ADR 0039
  made for every layer 1 path.
- An item PR changing a nested `.claude/` fails plan-scope, and merge-authority sends it to the
  admin.
- Consumers should still cover `**/.claude/**` in CODEOWNERS: shell writes are not confined by
  the session hook, and CODEOWNERS is the merge backstop. That is each consumer's own change.
- guard-security already matches `.claude/hooks/` and `.claude/settings*.json` at any depth, so
  the two guards now agree on nested settings and hooks.
- The change reaches a machine only when the instance pin moves past it and bootstrap runs.

## Measurements

Before the change, the new cases fail: two Python tests (plan-scope and merge-authority) and
seven guard-paths cases. After it, the Python suites for both modules and the full guard suite
pass.
