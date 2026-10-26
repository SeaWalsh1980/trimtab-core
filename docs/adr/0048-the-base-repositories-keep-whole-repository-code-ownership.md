# 0048. The base repositories keep whole-repository code ownership

## Status

Accepted on merge. Supersedes, for trimtab-core and Trimtab only, the
plan in ADR 0043
(`docs/adr/0043-merge-authority-check-decides-whose-approval-a-pr-needs.md`,
which is not edited) to narrow CODEOWNERS to the admin-only paths in each
repository. Changes no control: CODEOWNERS stays as it is. Trimtab
records the same decision in its own ADR.

## Context

ADR 0043 lets the approver account approve and merge a plan run's item PRs,
with the admin's approval required only on admin-only paths. It planned a
SEC-7 PR in each repository to narrow CODEOWNERS from every path to those
paths, so that code-owner review would stop requiring the admin elsewhere.

trimtab-core and Trimtab are the base every adopting project runs.
A change to either one changes the guards, rules and checks of every
project, so a merge without the admin would let an automated chain change
the controls that confine it. The operator decided that every PR to these
two repositories needs the admin, and that approver merges belong only to
adopting projects, such as ControlTower.

## Decision

- CODEOWNERS in trimtab-core and Trimtab stays `*` owned by the admin.
  The ruleset's code-owner review then requires the admin on every PR.
- No approver login is configured here: the operator unset
  `TRIMTAB_APPROVER` and removed the approver account as a collaborator.
  merge-authority therefore needs the admin's approval on every PR, plan
  head or not.
- Narrowed CODEOWNERS, and approver merges of item PRs, apply only to
  adopting projects, each through its own CODEOWNERS (a template and a
  check for it are still to be built).

## Options considered

| Option | Why it lost |
|---|---|
| Narrow CODEOWNERS to the admin-only paths, as ADR 0043 planned | An approved plan run could then merge changes to the base that every project runs, with no admin review; the admin-only list cannot name every path whose change alters what another project enforces |
| Drop CODEOWNERS and rely on merge-authority with no approver | merge-authority runs the PR's own copy of its workflow, so a PR that edits the workflow can make it pass; only a rule GitHub enforces outside the PR holds against that (ADR 0043) |
| Narrow CODEOWNERS but list more paths (agents, commands, hooks, routines, ADRs) | Every path in a base repository is part of what other projects run, so the list converges on `*` and adds a way to miss one |

## Consequences

- Every PR to trimtab-core and Trimtab needs the admin, as before ADR
  0043. Code-owner review is the gate; merge-authority is a second layer
  and a live test of the checker adopting projects run.
- A plan run can still work here. Its item PRs stop at the admin's merge,
  so the merge the wrap-merged skill's `merged_by` check reads is always
  the admin's.
- No CODEOWNERS SEC-7 PR follows in either base repository. An adopting
  project's CODEOWNERS template starts from ADR 0043's admin-only list.
- plan-scope and merge-authority still run on every PR here and cost
  Actions minutes for a gate code-owner review already holds. Whether to
  keep them here is left to the operator's Actions-usage review.
