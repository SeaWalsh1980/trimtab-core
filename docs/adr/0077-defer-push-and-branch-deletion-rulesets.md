# 0077. Defer the `.github/**` push ruleset and the branch-deletion ruleset

## Status

Proposed (deferred). Decided by the operator on 2026-11-13/14. Covers items 2 and 9 of the
2026-11-12 repo protection-gaps plan (gap 2a for the push ruleset). Nothing was changed.

## Context

Item 2 is a push ruleset that refuses pushes touching `.github/**`, so a changed workflow
cannot reach any branch without the admin. GitHub's documentation lists push rulesets
for private repositories under GitHub Team or Enterprise. The operator's personal Pro
account offers only branch and tag rulesets. Item 9 is a ruleset that stops branch
deletion; it would also refuse in-flight sessions' routine branch deletions after merge.

The operator plans to move both repositories into an organization on Team, but is holding
the move until their other in-flight sessions on these repositories finish.

## Decision

Defer both rulesets to that move. When the push ruleset lands:

- Rule "Restrict file paths" with the pattern `.github/**/*`, because `.github/**` alone
  does not match recursively.
- Bypass: Repository admin, plus Dependabot (operator's decision, so GitHub Actions
  version bumps keep arriving). Dependabot's behaviour is fixed by
  `.github/dependabot.yml`, which the ruleset itself locks.

The branch-deletion ruleset waits for the same move so it does not refuse deletions made
by in-flight sessions.

## Interim position

- The local agent token has no Workflows permission, so GitHub refuses its pushes of
  workflow files.
- The CODEOWNERS admin review is the merge gate for everything under `.github/`.
- Still open: other `.github/` files (`CODEOWNERS`, `dependabot.yml`, `.github/scripts/`)
  can be pushed to a branch by the agent but take effect only through a merge. The
  cloud-session route through the Claude App's workflow permission is untested (ADR 0076).

## Options considered

- **A required path check from the repository's own workflows.** Rejected: any workflow
  can post a check of the same name from the same App, so it is forgeable by the very
  change it should stop.
- **A `pull_request_target` workflow doing the path check.** Rejected: it runs with
  secrets next to untrusted PR code.
- **Make Trimtab public** (trimtab-core already is). Rejected: trimtab-core, already public
  on Pro, is reported by the operator as not offered push rulesets (the agent cannot see
  the UI), so going public likely would not close the gap. Second reason: it publishes a
  private harness to close a gap that CODEOWNERS already covers at merge.
- **Add the branch-deletion ruleset now.** Rejected for now: it would refuse in-flight
  sessions' branch deletions; there is no matching security gain while the default-branch
  ruleset already blocks deleting `main`.

## Consequences

- Accepted: until the move, a pushed branch can carry a non-workflow `.github/` change;
  only review at merge stops it taking effect.
- Review this record when the move happens; supersede it with the record of the ruleset
  as set.
