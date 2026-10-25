# 0040. Write-allowlist entries can be create-only

## Status

Accepted on merge. Amends ADR 0039
(`docs/adr/0039-guard-paths-refuses-the-harness-and-enforces-a-write-allowlist.md`)
by adding one entry form to the write allowlist; the rest of ADR 0039
stands, and ADR 0039 itself is not edited. This changes a security control
(`hooks/guard-paths.sh`); the operator authorised it in chat.

## Context

A repository's write allowlist (ADR 0039) grants a path or refuses it, with
nothing between. The operator wants some records to be writable only as new
files:

- **Decision records.** ENG-7.3 prohibits rewriting an accepted ADR; a
  superseded one is marked and linked forward. The operator's rule is that
  an ADR is rewritten only at a person's request, never on an agent's own
  initiative.
- **Progress records of an authorised plan.** The instance's agent-workflow
  rule prohibits editing an earlier progress record; each item adds its own.

A plain allowlist entry such as `docs/adr/**` lets an agent edit any of
those files, and the guard could not tell creating one from rewriting one.

## Decision

An allowlist line starting with `+` (spaces around it ignored) is
**create-only**. For a path it matches:

- a **Write that creates a new file** passes;
- an **Edit, MultiEdit or NotebookEdit**, or a **Write over a file (or link)
  that already exists**, is refused, with a message naming ADR 0040.

A plain entry that matches the same path wins, since it grants more. The
operator's `HOOK_ALLOW_HARNESS=1` export lifts create-only with the rest of
the harness layer, which is the elevated mode for a requested rewrite or
for marking an ADR superseded. CODEOWNERS already gives these paths to the
owner and the rulesets require code-owner review, so a rewrite that reaches
a pull request still needs the owner's approval.

## Options considered

| Option | Why it lost |
|---|---|
| Leave ADRs and progress records out of the allowlist | An agent could then not write a new ADR or its own progress record, which the plan-run rule requires |
| A separate append-only list file | A second file to protect and keep in step; a prefix on the existing list says the same thing in one place |
| Detect edits from content (only allow additions) | The guard sees a path, not the change; diffing content is semantic detection the guard's design rejects |
| Rely on CODEOWNERS alone | The owner reviews the merge, but the rewrite already happened in the session; the operator asked for it to be stopped there |

## Consequences

- Marking an ADR superseded, or correcting a typo in one, needs the
  operator's `HOOK_ALLOW_HARNESS=1`, by design.
- An agent that writes a new file under a create-only path cannot revise it
  later in the same session with Edit; it has to get it right in the Write,
  or ask the operator.
- Shell writes are not confined by the allowlist (ADR 0039's residual): a
  `sed -i` on an existing ADR is not refused here. CODEOWNERS at merge is
  the backstop.
- The Trimtab instance's allowlist uses create-only entries for its decision
  records and plans; trimtab-core's for its decision records.
