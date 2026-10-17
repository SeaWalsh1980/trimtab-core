# 0029. guard-mcp passes three PR-workflow writes by exact full name

## Status

Accepted (2026-10-17), by the operator. Amends ADR 0013
(`docs/adr/0013-guard-mcp-asks-before-mcp-tools-that-may-write.md`) and
ADR 0027 (`docs/adr/0027-guard-mcp-passes-desktop-app-tools-by-exact-name.md`),
which kept `mcp__ccd_session_mgmt__send_message` off its list; the rest of
both stands. Departs from ADR 0020
(`docs/adr/0020-routines-refuse-the-remote-session-mcp-tools.md`) for
interactive sessions only; routines still refuse the tool. See "PR activity,
and ADRs 0020 and 0028".

## Context

A session that opens a pull request then drives it: it edits the PR's title
and body as the work changes, subscribes to the PR's activity so CI results
and review comments wake it, and hands work to or reports to a sibling
session. Three tools do that, and none starts with a read verb, so guard-mcp
asks on every call:

- `mcp__github__update_pull_request` edits a PR: title, body, base branch,
  state, draft flag, reviewers.
- `mcp__claude-code-remote__subscribe_pr_activity` delivers a PR's comments,
  reviews and CI results into the session as events.
- `mcp__ccd_session_mgmt__send_message` posts a message into another desktop
  session.

The cloud platform tells a session to subscribe to every PR it creates. With
the ask in place, an attended session prompts for it on each PR and an
unattended one is refused, since an ask with no one present ends as a denial
(ADR 0013). The body edits come on every round of review, and the prompts
train the approval without reading that ADR 0013 rejected "ask on every MCP
call" for.

A permission rule cannot fix this: guard-mcp's ask stands whatever a settings
`allow` rule says (ADR 0027, amendment).

## Decision

guard-mcp passes the three by exact full name, a literal string equality,
never a prefix, suffix, glob or `<tool>`-part match.

- `mcp__ccd_session_mgmt__send_message` joins `CCD_ALLOW`, since the desktop
  app fixes its server's name (ADR 0027).
- `mcp__github__update_pull_request` and
  `mcp__claude-code-remote__subscribe_pr_activity` go on a new list,
  `PR_WORKFLOW_ALLOW`, checked right after `CCD_ALLOW` and before the name is
  split. The server part is matched as well as the tool part.

Every other tool on those servers keeps asking: `merge_pull_request`,
`update_pull_request_branch`, `unsubscribe_pr_activity`, the remote server's
own `send_message` and `create_session`, and the app's `delete_session`,
`set_session_permission_mode` and the rest of ADR 0027's off-list tools.

guard-mcp stays never-deny for these names. Nothing in this change exits 2.

### PR activity, and ADRs 0020 and 0028

ADR 0020 refuses the remote server's tools in routines, because PR events are
untrusted text that arrives after a run's work while it can still write.
ADR 0028 narrowed the desktop app's Auto-fix to PRs the session created, for
the same reason. This record passes `subscribe_pr_activity` for any PR
number: guard-mcp reads names, not arguments (ADR 0013), and the tool takes
an owner, repository and number, not the URL that ADR 0028's recorder
matches.

The routine templates still list the tool in `disallowed_tools`, and a
disallowed tool is taken out of the session, so no hook is reached and
routines keep ADR 0020's refusal. The pass reaches interactive and other cloud sessions, which the
operator chose to let follow their own PRs without a prompt.

## Options considered

- **A settings `allow` rule in the instance.** Rejected. It does not remove
  the prompt, because the hook's ask stands, and a per-instance pass is the
  widening by data that ADR 0027 rejected.
- **`HOOK_ALLOW_MCP=1`.** Rejected. It turns guard-mcp off for every MCP
  call.
- **Match on the `<tool>` part on any server, as ADR 0013's verb-last reads
  do.** Rejected. These are writes, so a tool of the same name on another
  server would write unprompted. The cost is that a deployment naming the
  GitHub server otherwise (the plugin's `plugin_github_github`) still asks.
  A second full name can be added when one is seen.
- **Pass the whole `claude-code-remote` or `ccd_session_mgmt` server.**
  Rejected. Both carry tools that create, archive or delete sessions.
- **Limit `subscribe_pr_activity` to PRs the session created, as ADR 0028
  does for `set_monitor`.** Not taken now. The recorder matches PR URLs from
  `gh` and REST creates, and cloud sessions create PRs through the GitHub
  MCP server, which it does not see. Deferred until a recorder covers
  that path.
- **Leave it.** Rejected by the operator.

## Trial

Negative controls, on a copy of the hooks on 2026-10-17, with the guard
matrix run against each copy:

- The `PR_WORKFLOW_ALLOW` comparison replaced with a `<tool>`-part match: the
  other-server case (`plugin_github_github__update_pull_request`) and the
  server-prefix case (`xgithub`) failed.
- Replaced with a match on the whole `github` and `claude-code-remote`
  servers: the suffix, case, merge, unsubscribe, remote `send_message` and
  `create_session` cases failed.
- `merge_pull_request` added to the guard's list alone: the list-equality
  case failed, and so did the merge case.

The pr-monitor cases also failed on those copies, because the copied hooks
directory sits outside the checkout that holds the Python package; they pass
against the checkout. On the shipped list the matrix passes.

## Consequences

- The three run without a prompt from guard-mcp in every mode, unattended
  runs included, except in routines, whose templates refuse
  `subscribe_pr_activity`.
- Accepted: `update_pull_request` can close a PR, change its base branch or
  flip its draft flag without a prompt. Marking a PR ready is what starts
  the review in an instance that drafts every PR, so a session can now do
  that unprompted. Merging still asks.
- Accepted: a session can subscribe to any PR's activity and then receives
  its comments and CI output, untrusted text, while it can push. That is
  wider than ADR 0028 allows `set_monitor` on the desktop.
- Accepted: a session can send a message into any other desktop session. The
  message reaches that session as text from a peer, and can carry what the
  sender read from a PR or issue.
- Accepted residual: the names are owned by their vendors. A listed name
  reused for a tool with another effect would pass. A renamed tool falls
  back to the classification and asks, which fails closed.
- The tests pin `PR_WORKFLOW_ALLOW` against the guard, as for `CCD_ALLOW`.
  `mcp__ccd_session_mgmt__delete_session` takes `send_message`'s place among
  the off-list app tools, so that list keeps a session-management write.
- CI steps that run the base branch's guard matrix against this branch's
  hooks go red on the pull request that makes this change, because the
  base's matrix expects `send_message` to ask. That is by design.
