# 0075. Actions: approval for all external contributors' fork workflows

## Status

Accepted. Changed by the operator in the GitHub UI on 2026-11-13/14. Actions
configuration is a SEC-7 control; this record documents the operator's change.

## Context

Workflows triggered by a fork's pull request run only after approval, by a repository
setting. It was "Require approval for first-time contributors". Under that setting, once
one PR from an account has been approved, that account's later fork PRs run unattended.
The repository is public, so anyone can open a fork PR; it has no regular outside
contributors today.

## Decision

Set fork pull request workflow approval to **Require approval for all external
contributors**.

Verified as already set, and left unchanged: default workflow permissions are read-only,
and "Allow GitHub Actions to create and approve pull requests" is off. Also checked,
read-only, alongside: the `BranchProtection` bypass list is Repository admin for pull
requests only, and the repository has no webhooks and no deploy keys.

Verification limits: these Actions settings, the bypass list, the webhooks and the deploy
keys are operator-reported from the UI. The agent's token cannot read them (the Actions
permissions endpoints return 403, the hooks list needs `admin:repo_hook`, the deploy-keys
list and bypass actors need admin).

## Options considered

- **First-time contributors only.** Rejected: one approved PR unlocks unattended runs for
  that contributor for good, and approval is the only gate before a fork's PR triggers
  workflows here.
- **No approval required.** Never considered viable for a repository whose workflows hold
  review and merge-authority logic.

## Consequences

- Each external contributor's PR waits for a maintainer click before any workflow runs.
  Accepted: friction, which is nil while no outside contributor exists.
- The read-only default and the off switch for Actions-created approvals stand, so a
  workflow cannot approve its own PR.
