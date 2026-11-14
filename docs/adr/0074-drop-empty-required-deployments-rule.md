# 0074. Drop the empty `required_deployments` rule from `BranchProtection`

## Status

Accepted. Changed by the operator in the GitHub UI on 2026-11-13/14. Repository
protection is a SEC-7 control; this record documents the operator's change.

## Context

Ruleset `BranchProtection` (id 24363463, target `~DEFAULT_BRANCH`) carried a
`required_deployments` rule with no environments listed. The repository has no
environments, so the rule required nothing, but in a settings review it read as a
deployment gate that did not exist.

## Decision

Remove the `required_deployments` rule. Nothing else in the ruleset changes.

Read back after the change from `gh api .../rulesets/24363463`, enforcement active:

- `deletion`, `non_fast_forward`.
- `pull_request`: 1 approving review, code-owner review required, last-push approval
  required, stale reviews dismissed on push, review-thread resolution required, extra
  approval for unattributed changes, merge methods merge/squash/rebase.
- `required_status_checks`: `lint-test`, `GitGuardian Security Checks`, `claude-review`,
  `plan-scope`, `merge-authority`.
- `strict_required_status_checks_policy: false`. "Require branches to be up to date" is
  OFF. It was turned off on 2026-11-12 for the approver work, not in this change; it is
  recorded here because this is the first record to state the full ruleset.

## Options considered

- **Keep the empty rule.** Rejected: it enforces nothing and misleads a reviewer into
  believing a deployment gate exists.
- **Add a real environment to give it meaning.** Rejected: there are no deployments to
  gate; an environment would be invented to justify a rule.

## Consequences

- No change to merge behaviour; the rule required nothing.
- Accepted: with strict off, a PR can merge on checks that ran against an older base. The
  window is accepted for now (turned off 2026-11-12 for the approver work); revisit if a
  stale-base merge breaks main.
