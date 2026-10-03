# 0009. Dependabot bumps the pinned actions

## Status

Accepted.

## Context

Both workflows pin their actions by full commit SHA, with the version in a
trailing comment: `actions/checkout` (v4.4.0) in `ci.yml` and
`claude-code-review.yml`, and `anthropics/claude-code-action` (v1.0.230) in
`claude-code-review.yml`. A SHA pin never moves on its own, so without
something to propose updates the pins only age.

The claude-code-action pin matters most. `claude-code-review.yml` sets no
model, and the action passes none either: it installs a fixed Claude Code
version, whose default model does the review. The reviewing model therefore
changes only when the action pin does.

The constraints:

- Action code runs with the workflows' tokens, so a new version must arrive
  as a change someone reviews, not by itself.
- No new infrastructure unless it is needed.
- The repository has no Python dependency manifest; the only pinned
  dependencies are the actions.

## Decision

- **Leave the review model unset**, so it follows the action pin.
- **Add `.github/dependabot.yml`** for the `github-actions` ecosystem only,
  checked on Mondays at 07:00 UTC. Minor and patch bumps are grouped into
  one PR; a major bump arrives in a PR of its own; at most 5 are open at
  once. Commits are `ci(deps): …`.
- **Keep every pin's comment to the bare `# vX.Y.Z`.** Dependabot rewrites
  the comment only when the version is its last token; anything after it
  leaves the comment stale while the SHA moves.

A new action version, and with it any new default model, then arrives as a
PR to review rather than as a hand edit.

## Options considered

- **Pass an explicit `--model` ID to the action.** Rejected. Every new
  model would need a hand edit, and the pinned Claude Code may not know a
  newer ID anyway.
- **Pass the `sonnet` alias.** Rejected. The alias is resolved by the same
  pinned Claude Code, so it moves no more often than the default does.
- **Track the action by a moving tag such as `@v1`.** Rejected. Unreviewed
  action code would run with the workflows' tokens.
- **Bump the pins by hand.** Rejected. Nothing prompts the bump, so in
  practice the pins only age.

## Consequences

- Action updates arrive weekly as reviewable PRs.
- Accepted: every action used here appears in `claude-code-review.yml`, so
  each Dependabot PR edits that workflow and claude-review skips it while
  still reporting success. The operator reviews every one.
- Accepted: a claude-code-action bump can change the reviewing model even
  though the PR names only the action version. The reviewer checks the
  action's release notes for the Claude Code version it installs.
- An `ignore` entry added later would also suppress security updates if
  they are switched on, so the file asks for any such entry to say so.
