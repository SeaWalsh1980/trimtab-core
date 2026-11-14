# 0079. Actions allow-list: GitHub's own actions, `anthropics/claude-code-action` and `oven-sh/setup-bun`

## Status

Accepted. Changed by the operator in the GitHub UI on 2026-11-14 (Settings → Actions →
General → Actions permissions). Actions configuration is a SEC-7 control; this record
documents the operator's change and changes no file or setting. Related:
[0073](0073-tag-ruleset-tagprotection.md) to [0077](0077-defer-push-and-branch-deletion-rulesets.md)
(the 2026-11-13/14 settings records; [0075](0075-actions-fork-approval-all-external-contributors.md)
recorded the other Actions settings, and [0077](0077-defer-push-and-branch-deletion-rulesets.md)
holds gap 2a) and [0078](0078-require-branches-up-to-date-is-off-on-branchprotection.md). No
earlier record lists the allow-list as open in this repository, so none is edited.

## Context

Before the change (operator-reported) any action from anywhere could run in this repository's workflows. An action runs
inside the job with the job's `GITHUB_TOKEN` and every secret the job can read, so a
malicious or hijacked third-party action is code execution with those credentials.

The repository's workflows use three actions, each pinned by full commit SHA (checked on
`origin/main` at 655603f): `actions/checkout`, `actions/setup-python` and
`anthropics/claude-code-action`. Dependabot keeps those pins current
(`.github/dependabot.yml`).

`claude-code-action` is a composite action and has a nested `uses:`. Read from its `action.yml`
at the pinned SHA 4fa93a8: `oven-sh/setup-bun@0c5077e` (v2.2.0), pinned by SHA at that action's
own pin. It is not GitHub's. GitHub's allow-list applies to such transitive actions too, and
that is now confirmed live. Trimtab's first `claude-review` failed at setup with
"oven-sh/setup-bun not allowed" until the operator listed it (Trimtab ADR 0038, merged in its
#217). Here, `claude-review` run 43575848775 on this PR then downloaded and ran
`oven-sh/setup-bun@0c5077e` successfully, which is consistent with it being listed here given the operator-reported restriction. It is not independent proof that the restriction is active: the agent token gets 403 on `actions/permissions`.

The repository is public and owned by a personal account on GitHub Pro.

## Decision

Set, as the operator reported them:

- Actions permissions: "Allow SeaWalsh1980, and select non-SeaWalsh1980, actions and
  reusable workflows".
- "Allow actions created by GitHub": on.
- "Allow actions by Marketplace verified creators": off.
- Allow list: `anthropics/claude-code-action@*`, `oven-sh/setup-bun@*` (the second added by
  the operator for the transitive use above; run 43575848775's success is the evidence).
- "Require actions to be pinned to a full-length commit SHA": **on** (corrected 2026-11-14,
  see Correction). It was first recorded here as unconfirmed.

Unchanged: default workflow permissions are read-only, Actions cannot create or approve
pull requests, and fork PR workflows need approval for all external contributors
([0075](0075-actions-fork-approval-all-external-contributors.md)).

Verification limit: these are operator-reported from the UI. The agent's token gets 403 on
the `actions/permissions` endpoints and cannot read them.

## Options considered

| Option | Why it lost |
|---|---|
| Allow all actions and reusable workflows | Any action from anywhere runs with the job's token and secrets. It admits the whole Marketplace for the sake of three direct actions plus one nested. |
| Allow only actions in this account (no GitHub, no third party) | Breaks all three used actions: `actions/checkout`, `actions/setup-python` and `anthropics/claude-code-action` are not this account's. |
| GitHub's actions plus Marketplace verified creators | Verified means GitHub knows the publisher, not that an action is safe, and it admits every action from every verified creator, far more than is used. The non-GitHub actions needed are named instead. |
| Disable Actions | The required checks (`lint-test`, `claude-review`, `plan-scope`, `merge-authority`) run on it; no PR could merge. |

## Consequences

Accepted:

- A `claude-code-action` bump that adds a new nested action fails `claude-review` at setup
  until the admin lists it. Dependabot bumps of the listed actions keep working only while
  their nested actions stay on the list.
- With "Require actions to be pinned to a full-length commit SHA" on, any `uses:` ref that
  is not a full SHA fails at job start. That includes a nested action that a
  `claude-code-action` bump brings in by tag. The fix is to re-pin it, not to switch the
  setting off.
- Adding a new third-party action needs the admin to add it to the list first. Until then a
  workflow using it fails at job start with an "action is not allowed" error.

What this does NOT do:

- It does not stop a workflow edit from running arbitrary code. A changed workflow can run
  anything through a plain `run:` step, with no third-party action involved. The allow-list
  limits which actions load, not what the workflow's own steps do.
- It does not stop a compromised new release of an allowed action. The new release arrives
  through a Dependabot bump of the pin, and the list admits every ref of
  `anthropics/claude-code-action`; `oven-sh/setup-bun@*` is likewise admitted at every ref, and its ref changes only through a `claude-code-action` bump. Review of that bump covers both.
- It is therefore supply-chain hygiene, not a fix for gap 2a (a changed workflow reaching a
  branch). Gap 2a stays deferred to the organization move
  ([0077](0077-defer-push-and-branch-deletion-rulesets.md)); CODEOWNERS admin review of
  `.github/` at merge remains the gate meanwhile.

Verified: run 43575848775 (this PR's `claude-review`) shows only that the allowed actions,
including the nested `oven-sh/setup-bun`, work under the list. It does not show the restriction
is active (corrected 2026-11-14, see Correction). Trimtab's first `claude-review` run failed
at setup with "oven-sh/setup-bun not allowed" until the operator listed it (Trimtab ADR 0038,
merged in its #217). That shows GitHub enforces allow-lists on nested actions; that this
repository's list is active rests on the operator's report. A blocked action fails at job start
with "not allowed"; if one does, add the action to the list (admin) or revert the setting.

## Correction

2026-11-14. Two points amended; two passages were edited in place (the "Require actions to be
pinned" bullet in Decision, and the "Verified" paragraph), because leaving them would make
this record contradict itself. One Consequences bullet was added for the pinning setting.
Nothing else changed.

1. SHA pinning: the operator confirmed in chat on 2026-11-14 that the setting was offered and
   is ticked on this repository. It was recorded as unconfirmed. Every action the workflows use,
   including the nested `oven-sh/setup-bun`, is already pinned by full SHA, so nothing broke.
2. "Verified": the text said every job started under the allow-list, as if that showed the
   restriction is active. A passing run shows only that the allowed actions work. Trimtab's
   first `claude-review` failing at setup with "oven-sh/setup-bun not allowed" (Trimtab ADR
   0038, #217) shows GitHub enforces allow-lists on nested actions; this repository's list
   being active rests on the operator's report.
