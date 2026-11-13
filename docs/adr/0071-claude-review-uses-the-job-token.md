# 0071. claude-review uses the job's own token, not the Claude App's

## Status

Proposed. This changes a SEC-7 control: the workflow that runs `claude-review`
(`.github/workflows/claude-code-review.yml`). The operator authorised the change in chat
("Start 9c" of the 2026-11-12 repo protection-gaps plan). `claude-review` cannot be
relied on to review the PR that carries it. Its skip for workflow-editing PRs lived in the
App token exchange, which this PR's own copy of the workflow no longer makes, so on ready
it will probably run under this PR's own prompt and tools. A green result is then a
self-review, not a substitute for the human review of the diff the admin does before
merging (CODEOWNERS `*`).

## Context

`claude-code-action` obtains its GitHub token by exchanging the job's OIDC token
(`id-token: write`) for a token of the Claude GitHub App. That App holds read/write on
workflows and repository hooks across every repository it is installed on. The reviewer is
comment-only (contents read, PR comments), yet its token can edit workflows and hooks, so
a prompt-injected review run has far more reach than its job needs.

## Decision

Pass `github_token: ${{ github.token }}` to the action and drop `id-token: write` from the
job's permissions. The token is then the job's own, capped by the declared `permissions:`
(`contents: read`, `pull-requests: write`, `issues: read`), and expires with the job.
Nothing else in the workflow changes.

Confirmed from the pinned action (`61a9c274…`, v1.0.269):

- `action.yml` documents `github_token` and passes it as `OVERRIDE_GITHUB_TOKEN`.
- `src/github/token.ts` `setupGitHubToken()` returns `OVERRIDE_GITHUB_TOKEN` when set and
  returns before `getOidcToken()` and the exchange: no OIDC request is made.
- `src/entrypoints/prepare.ts` runs the write-permission check on the actor with that
  token; the PR author has write access in this repo, as before.

The end-state assertion (ADR 0069) still holds. It filters issue comments on
`user.type == "Bot"`, a body containing the run URL and starting `**Claude finished `.
`github-actions[bot]` has type `Bot`, and the tracking comment text does not depend on
the token. No change to the assertion.

## Options considered

- **Keep the App token.** Rejected: leaves workflows/hooks write on a token handed to an
  agent that reads untrusted PR content.
- **App token plus `additional_permissions`.** Rejected: that input only widens the
  exchanged token's permissions; it cannot narrow it below the App's grant.
- **Uninstall the Claude GitHub App outright.** Not now. It is this repo's fallback once
  9c works everywhere (Trimtab too), and is an operator action in the GitHub UI, tracked
  as a separate item.

## Consequences

- The reviewer's token can no longer touch workflows or hooks, and no `id-token` is
  requested.
- Comments post as `github-actions[bot]`, not `claude[bot]`.
- **Accepted, loss of an anti-tampering check.** The App exchange is what made the action
  skip a PR that edits the reviewer workflow (`WorkflowValidationSkipError`, token.ts).
  Without the exchange that check no longer applies, and a `pull_request` run uses the PR's
  own copy of the workflow, so a PR could change the prompt or `--allowedTools` and be
  reviewed under its own terms. The blast radius is now bounded by the job token
  (comment-only), and a PR that edits `.github/workflows/` still needs the admin's
  review under CODEOWNERS. ADR 0069's consequence "a PR that edits the workflow still goes
  unreviewed by CI and still fails the check" no longer holds: such a PR can now go green.
  This was never a forgery barrier (the PR could always rewrite the assertion step in the
  same file); what changed is that the honest default flipped from red to green. The workflow's existing comments about the skip describe the
  old behaviour; they are left unchanged here and are a follow-up.
- `issues: read` is kept. GitHub allows PR-conversation comments with `pull-requests:
  write`. If the first post-merge run is denied (403) on the tracking comment, the
  assertion fails closed and `issues: write` is the next step.
- Not run-proven until the first core PR reviewed after merge.

## Verification after merge

On the next core PR: the review and tracking comment are authored by `github-actions[bot]`,
the "Fail closed when no review was posted" step passes, and the job log shows
"Using provided GITHUB_TOKEN" with no "Requesting OIDC token".

## Follow-up note

The workflow comments and failure message that described the skip were corrected after
this record merged: a PR that edits the workflow runs its own copy of the assertion, the
prompt and the tool allowlist, so `claude-review` carries no information about such a PR
either way, and the admin's CODEOWNERS review of the diff is the only gate there. The
decision above is unchanged.
