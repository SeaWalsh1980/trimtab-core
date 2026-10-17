# 0030. guard-mcp passes three PR-workflow writes by exact full name

## Status

Accepted (2026-10-17), by the operator. Amends ADR 0013
(`docs/adr/0013-guard-mcp-asks-before-mcp-tools-that-may-write.md`), adding a
second tool whose arguments guard-mcp reads, and ADR 0027
(`docs/adr/0027-guard-mcp-passes-desktop-app-tools-by-exact-name.md`), which
kept `mcp__ccd_session_mgmt__send_message` off its list; the rest of both
stands. Departs from ADR 0020
(`docs/adr/0020-routines-refuse-the-remote-session-mcp-tools.md`) outside
routines only; routines still refuse the subscription. See "PR activity, and
ADRs 0020 and 0028".

## Context

A session that opens a pull request then drives it: it edits the PR's title
and body as the work changes, subscribes to the PR's activity so CI results
and review comments wake it, and hands work to or reports to a sibling
session. Three tools do that, and none starts with a read verb, so guard-mcp
asks on every call:

- `mcp__github__update_pull_request` edits a PR: title, body, reviewers, base
  branch, state, draft flag, and whether maintainers may push.
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

One thing the ask on `update_pull_request` does carry. An instance may hold
its sessions to opening every PR as a draft and marking it ready only once
the branch is final, because marking it ready starts the review. Through the
MCP route, this guard's ask is what enforces that, and a rule that must hold
whatever the model decides has to be enforced by a hook, permission rule or
test, not by instruction text alone.

A permission rule cannot remove the prompt on its own: guard-mcp's ask stands
whatever a settings `allow` rule says (ADR 0027, amendment). The reverse also
holds: a pass from guard-mcp is no decision, so the session's permission
rules still decide whether the call prompts.

## Decision

guard-mcp passes the three by exact full name, a literal string equality,
never a prefix, suffix, glob or `<tool>`-part match. The server part is
matched as well as the tool part.

- `mcp__claude-code-remote__subscribe_pr_activity` and
  `mcp__ccd_session_mgmt__send_message` go on a new list,
  `PR_WORKFLOW_ALLOW`, checked after `CCD_ALLOW` and before the name is
  split. `send_message` stays off `CCD_ALLOW`, whose rule is that its tools
  only drive the app's UI or read its state; a message into another session
  is a write, so it sits with this record's other passes.
- `mcp__github__update_pull_request` goes on a new list, `PR_EDIT`, checked
  just before `PR_WORKFLOW_ALLOW`. For that name only, guard-mcp reads the
  call's arguments. It passes when `tool_input` is an object whose keys are
  all among `owner`, `repo`, `pullNumber`, `title`, `body` and `reviewers`.
  An empty object, or one naming only the PR, passes too: it changes
  nothing. Anything else asks: `draft` either way, `state`, `base`,
  `maintainer_can_modify`, an argument a later release adds, arguments that
  are not an object, and a call with no `tool_input` at all. The comparison
  runs inside the parser (jq, or isolated python3 without it), so a key is
  never split, echoed or put in a reason. The ask names its cause: an
  argument off the list, no argument object, or a check that could not
  finish, which says nothing about what the call changes.

The key list is an allowlist, not a list of keys to refuse, so a new argument
asks until it is reviewed. The call asks rather than denies, unlike
`set_monitor` in ADR 0028: marking a PR ready is a step the operator takes on
purpose, so the call waits for them rather than being refused.

Every other tool on those servers keeps asking: `merge_pull_request`,
`update_pull_request_branch`, `unsubscribe_pr_activity`, the remote server's
own `send_message` and `create_session`, and the app's `delete_session`,
`set_session_permission_mode` and the rest of ADR 0027's off-list tools.

Nothing in this change denies or exits 2.

### PR activity, and ADRs 0020 and 0028

ADR 0020 refuses the remote server's tools in routines, because PR events are
untrusted text that arrives after a run's work while it can still write.
ADR 0028 narrowed the desktop app's Auto-fix to PRs the session created, for
the same reason. This record passes `subscribe_pr_activity` for any PR
number: the tool takes an owner, repository and number, not the URL that
ADR 0028's recorder matches, and the recorder does not see PRs created
through the GitHub MCP server.

The routine templates still list the tool in `disallowed_tools`, and a
disallowed tool is taken out of the session, so no hook is reached and
routines keep ADR 0020's refusal. The pass reaches interactive and other
cloud sessions, which the operator chose to let follow their own PRs without
a prompt.

## Options considered

- **A settings `allow` rule in the instance, alone.** Rejected. It does not
  remove the prompt, because the hook's ask stands, and a per-instance pass
  is the widening by data that ADR 0027 rejected. An instance that wants no
  prompt at all still needs its permission rules to allow the three, once
  this guard passes them.
- **`HOOK_ALLOW_MCP=1`.** Rejected. It turns guard-mcp off for every MCP
  call.
- **Pass `update_pull_request` by name, whatever its arguments.** Rejected
  after review. It removes the only enforcement of draft-until-final through
  the MCP route, and lets a session close a PR or change its base
  unprompted.
- **Leave `update_pull_request` off every list, so it always asks.**
  Rejected by the operator. Body and title edits are the frequent case and
  carry no review consequence.
- **Pass it by name and record the draft gap as an accepted risk.** Rejected.
  It leaves a rule that must hold whatever the model decides resting on
  instruction text alone.
- **A list of keys to refuse (`draft`, `state`, `base`).** Rejected. A
  maintainer flag, or any argument a later release adds, would pass
  unreviewed.
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
  MCP server, which it does not see. Deferred until a recorder covers that
  path.
- **Leave it as it is.** Rejected by the operator.

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

Those three ran while `update_pull_request` was on `PR_WORKFLOW_ALLOW`, and
the exactness cases used its name. Once it moved to `PR_EDIT` they no longer
exercised the list, so a second review found them protecting nothing. They
now use `subscribe_pr_activity` (`mcp__evil__…`, a `_x` suffix, an
`xclaude-code-remote` prefix, a case change), and were re-run against three
loosened copies of the list:

- a `<tool>`-part match: the other-server and prefix cases failed;
- a substring match on the tool and server: the suffix and prefix cases
  failed;
- a case-folded match: the case-change case failed.
- After review, `update_pull_request` moved to `PR_EDIT`. With its argument
  verdict ignored (pass by name): every ask case for it failed, ten in the
  main matrix plus the jq-only and python3-only ready cases.
- With the key allowlist replaced by a refusal of `draft`, `state` and
  `base` alone: the `maintainer_can_modify`, unknown-argument, two-keys-in-one
  and no-arguments cases failed.

The python3 fallback is run isolated (`python3 -I`), in both the name check
and the argument check. Without it, a `json.py` in the session's working
directory, the repository under work, shadowed the standard library when jq
was missing: a second review found it, and a plant that names every write a
read, and calls `draft: false` a title edit, passed both
`mcp__srv__create_item` and a ready-marking `update_pull_request` with no
prompt. The two cases that plant it failed on the unpatched guard and pass
with `-I`. The name check had the flaw before this record; it is fixed here
because the argument check shares it, and leaving it would keep a pass for
every MCP write.

The pr-monitor cases also failed on those copies, because the copied hooks
directory sits outside the checkout that holds the Python package; they pass
against the checkout. On the shipped lists the matrix passes, with jq and
with jq off the PATH.

## Consequences

- Neither `send_message` nor `subscribe_pr_activity` gets a prompt from
  guard-mcp, in any mode and unattended runs included, except that routines'
  templates refuse `subscribe_pr_activity`. Nor does `update_pull_request`
  when it edits only the title, body or reviewers. Whether a call then
  prompts is up to the session's permission rules.
- `update_pull_request` still asks when it marks a PR ready or draft, closes
  or reopens it, changes its base, sets the maintainer flag, or carries any
  argument outside the list. Draft-until-final stays enforced through the
  MCP route. A body edit sent together with `draft` asks as a whole.
- Accepted: a session can subscribe to any PR's activity and then receives
  its comments and CI output, untrusted text, while it can push. That is
  wider than ADR 0028 allows `set_monitor` on the desktop.
- Accepted: a session can send a message into any other desktop session. The
  message reaches that session as text from a peer, and can carry what the
  sender read from a PR or issue.
- Accepted: a session can request reviewers on a PR without a prompt.
- Accepted: the title, body and reviewer edits pass on any PR the session's
  credentials reach, not only one it opened: the check reads which fields
  change, not which PR. A session acting on injected text from a PR or
  issue could rewrite another PR's body unprompted, including an
  already-approved one, since editing a body does not dismiss an approval.
  Limiting the edit to the session's own PRs, as ADR 0028 does for
  `set_monitor`, is deferred with the subscription limit (Options
  considered): the recorder would have to log PRs created through the
  GitHub MCP server.
- Accepted residual: the names and argument names are owned by their
  vendors. A listed name, or a listed argument, reused for something with
  another effect would pass. A renamed tool falls back to the classification
  and asks, and a renamed or new argument asks, which fails closed.
- guard-mcp's python3 fallback runs isolated (`-I`), so neither a module in
  the working directory nor a `PYTHON*` variable can answer for it. The
  `set_monitor` check already ran with `-P` (ADR 0028).
- The bootstrap self-probe asks guard-mcp about a ready-marking
  `update_pull_request` and fails the install if it does not ask; a title
  edit that does not pass is a note. A test proves the install refuses a
  guard that passes the tool by name.
- A test fails when a name whose arguments are read (`CCD_OWN_PR`,
  `PR_EDIT`) also sits on a name-only list, or its tool part on the
  verb-last list, so its arguments cannot be skipped by a pass by name.
- The tests pin `PR_WORKFLOW_ALLOW` and `PR_EDIT` against the guard, as for
  `CCD_ALLOW`, and run the argument cases with jq only and with python3
  only. `mcp__ccd_session_mgmt__delete_session` takes `send_message`'s place
  among the off-list app tools, so that list keeps a session-management
  write. The bootstrap probes run the unparseable-payload probe once per
  hook, and one `probe_pass` helper carries the note-only pass probes.
- CI steps that run the base branch's guard matrix against this branch's
  hooks go red on the pull request that makes this change, because the
  base's matrix expects `send_message` to ask. That is by design.
