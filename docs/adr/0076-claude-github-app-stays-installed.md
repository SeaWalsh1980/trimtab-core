# 0076. The Claude GitHub App stays installed on all repositories

## Status

Accepted. This is an operator decision of 2026-11-13/14; no setting was changed. Repository
access is a SEC-7 control, so the decision is recorded.

## Context

Since ADR 0071 (`0071-claude-review-uses-the-job-token.md`) the `claude-review` workflow passes the job's `github.token`, so no
workflow in this repository takes the Claude GitHub App's token. The App still matters
elsewhere: cloud sessions started from the browser, routines and Projects threads clone
private repositories only through the App, and Auto-fix needs it. The operator runs cloud
automation that uses trimtab-core and Trimtab as reference projects. The operator also
states that the only active repositories are ones that use the App. The App holds
read/write on workflows and repository hooks wherever it is installed (same record). That record left uninstalling the App as a
separate follow-up; this record settles it.

## Decision

Leave the App installed with access to all repositories.

## Options considered

- **Install on selected repositories only.** Rejected for now: the operator judged it
  unnecessary while every active repository needs the App.
- **Uninstall outright.** Rejected: it breaks cloud sessions, routines and Auto-fix, and
  the CI of three other repositories that still use the App token.

## Consequences

- Accepted: a new repository gets the App automatically, with its read/write on
  workflows and repository hooks. If a repository that should not have it is created,
  switch the installation to "Only select repositories" first.
- Open risk, untested: whether a cloud session can push a workflow file through the App's
  workflow permission. If it can, that is a route to change `.github/workflows/` that only
  the CODEOWNERS admin review stops (see ADR 0077).
- Status of this decision should be revisited when the repositories move into an
  organization (ADR 0077), where installation scope is managed per organization.
