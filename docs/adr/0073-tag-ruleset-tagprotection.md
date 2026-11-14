# 0073. Tag ruleset `TagProtection` locks every tag

## Status

Accepted. The operator added the ruleset in the GitHub UI on 2026-11-13/14 (item of the
2026-11-12 repo protection-gaps plan). Repository protection is a SEC-7 control; this
record documents an operator-made change and changes no file.

## Context

The repository has no tags and no releases (read from the API: 0 and 0). Nothing resolves
a tag: the bootstrap shim pins trimtab-core by commit SHA, so no consumer would follow a tag. The ruleset therefore blocks nothing in use.
What it prevents is someone other than the admin creating a tag that a later consumer or
reader might trust, or moving one afterwards. The repository is public (read from the
API); the operator's account is on GitHub Pro, so the ruleset is enforced.

## Decision

Ruleset `TagProtection` (id 27254552), target tag, enforcement active, applies to all tags
(`~ALL`, no exclusions). Rules: `creation`, `update`, `deletion`, `non_fast_forward`.
Bypass: Repository admin, always. These values were read from
`gh api repos/SeaWalsh1980/trimtab-core/rulesets/27254552`; the bypass actor is
operator-reported, because the agent's token gets `bypass_actors: null` (it needs admin
to see the list).

## Options considered

- **No tag ruleset.** Rejected: the insurance costs nothing while no tag exists, and is
  costly to add after something has started trusting a tag, since a planted tag would
  already be in use.
- **Restrict deletions only.** Rejected: leaves tag creation and moves open, which are the
  routes by which a tag would be forged or repointed.

## Consequences

- Only the Repository admin can create, move or delete a tag. A future release process
  must run as the admin or the ruleset must be revisited.
- Accepted: the admin bypass means the protection is only as strong as the admin account.
- No effect on any current workflow, since none uses tags.
