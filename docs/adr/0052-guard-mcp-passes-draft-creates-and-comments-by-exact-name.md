# 0052. guard-mcp passes draft creates, comments, replies and the ready toggle by exact name

## Status

Proposed (2026-10-31), by the operator; accepted once the trial below is
recorded. Amends ADR 0034
(`docs/adr/0034-guard-mcp-passes-pr-workflow-writes-by-exact-name.md`), whose
ask on the draft flag goes, and ADR 0038
(`docs/adr/0038-guard-mcp-denies-the-pull-request-writes-the-operator-owns.md`),
whose draft create no longer falls through to an ask. The denies of ADR 0038
stand and still run first.

## Context

The operator wants autonomous cloud sessions that stop only for what needs a
person: a workflow change, SQL, or a review that still fails after its round
limit. A guarded cloud session cannot use `gh pr` (the GitHub proxy refuses
GraphQL, ADR 0021), so it works through the GitHub MCP server, and on
2026-10-31 one stopped at guard-mcp's final ask on `create_pull_request`. Every
PR would stop the same way, as would every comment, every reply and every ready
toggle. An ask with no one present ends as a denial (ADR 0013), so an
unattended run could not finish a PR at all.

ADR 0038 already reads `draft` and denies anything but the JSON `true`. A draft
create only reached the ask because nothing passed it.

Marking a PR ready starts a billed review. ADR 0034 kept that behind an ask, and
recorded that this ask is what enforces draft-until-final through the MCP route.
ADR 0038 calls ready the agent's own step. AgentWorkflows forbids a run marking
ready "other than through what the guards permit", and adds "(today the operator
does both)". The operator has decided it should not stop a run, and accepts that
the guard no longer holds draft-until-final on this route.

## Decision

guard-mcp passes four writes, each by exact full name and only when every
argument is on that tool's list:

| Tool | Arguments that pass |
|---|---|
| `mcp__github__create_pull_request` | `owner`, `repo`, `title`, `body`, `head`, `base`, `draft` |
| `mcp__github__update_pull_request` | `owner`, `repo`, `pullNumber`, `title`, `body`, `reviewers`, `draft` |
| `mcp__github__add_issue_comment` | `owner`, `repo`, `issue_number`, `body` |
| `mcp__github__add_reply_to_pull_request_comment` | `owner`, `repo`, `pullNumber`, `commentId`, `body` |

Anything else on those tools asks: closing a PR, a new base, the maintainer
flag, reviewers on a create, a reaction, an argument a later release adds,
arguments that are not an object, and a parser that does not finish. A non-draft
create is denied before these checks, as before.

The answer is the same in an interactive session and an autonomous run. guard-mcp
does not read the mode.

The routine templates list the four tools in `disallowed_tools`, so routines keep
today's refusal. Their prompts forbid marking a PR ready and writing outside the
project, and on the MCP route the ask was what held that. Routines work through
REST (ADR 0021), so they lose nothing. A live routine keeps its old list until it
is updated from its render.

The create pass reads `draft` again rather than rely on the deny before it, so a
guard that lost the deny refuses a non-draft instead of passing it.

Still denied: a merge, a non-draft create, an approval, and resolving a thread,
including the `resolve_thread` method of `pull_request_review_write`, which the
server's README now lists and ADR 0038 only anticipated. Still asking:
`pull_request_review_write` for everything else, `add_comment_to_pending_review`,
`update_issue_comment` and `discussion_comment_write`.

`bootstrap.sh` probed that a ready toggle asks, which would now fail every
install. It probes that closing a PR asks instead, which still catches a guard
that passes the tool by name.

## Options considered

- **Pass the draft create only, and leave comments and ready asking.** Rejected
  by the operator: each would still stop an unattended run.
- **Pass `pull_request_review_write` for a comment-only call** (`create` with no
  event, or exact `COMMENT` with listed keys). Rejected by the operator. One tool
  carries create, submit, delete, approve and resolve, and a review submit is a
  different act from a comment. Replies and top-level comments have their own
  tools.
- **Keep the ready toggle asking, or ask only in autonomous runs.** Rejected. The
  first stops every run at its last step. The second would make the step a run
  needs the one that fails, and would need the guard to read the mode.
- **Pass by name, whatever the arguments.** Rejected, as in ADR 0034: a reaction,
  a close or a new base would pass unreviewed.
- **A list of keys to refuse.** Rejected, as in ADR 0034: an argument a later
  release adds would pass.
- **Match on the `<tool>` part on any server.** Rejected, as in ADR 0034: these
  are writes, and the same name on another server is another tool. A deployment
  naming the server otherwise still asks.
- **Limit the target to the session's repository by its git remote.** Rejected:
  the session can change its remote, and the threat is a session acting on
  injected text.
- **Close further speculative resolve routes in this change** (other tool names,
  a generic GraphQL tool, `minimizeComment`). Not taken: the server's README lists
  none of them, and they ask today. ADR 0038's residuals stand.
- **A limit on target owners, from an operator-exported variable.** Held back
  until the reach check below is recorded. Not needed if the server already
  binds these calls to the session's repositories.

## Trial

On hook copies on 2026-10-31, loosening one thing each. Each failed the named
cases; the pr-monitor cases also fail on a copy, for the reason in ADR 0034.

- The create's key list ignored: maintainer flag, reviewers, unknown argument.
- The comment's key list ignored: reaction, `comment_id`, unknown argument, no
  object, and the rest of its ask cases.
- The reply's key list ignored: reaction, unknown argument.
- `state` added to the edit's list: closing, a draft with a close.
- The create deny's list emptied, before the pass read `draft` itself: a
  non-draft create passed silently. It is now refused, and the matrix holds that.
- A `<tool>`-part match on the create: other server, lookalike server.

On the shipped lists the matrix passes, with its jq-only and python3-only groups,
and pins the four argument lists. The base branch's matrix goes red on 11 cases, by design:
the ready toggle and draft create pins that used to ask (with their jq-only,
python3-only and planted-module versions), and the order check's line count.

**Pending, to be recorded here before this is accepted:**

- **Reach.** Whether a comment through the MCP server can land on a repository
  outside the session's sources.
- **The permission layer.** A pass from guard-mcp is no decision (ADR 0034).
  Whether a cloud session still prompts after a pass, which would need an allow
  rule in the instance.
- **A guarded cloud session** opening a draft with no stop, and being refused a
  non-draft create, a merge, an approval and a resolve.

## Consequences

- Rule text and the plan-run skill have not moved. `rules/AgentWorkflows.md`
  (in the instance) and `skills/wrap-merged/SKILL.md` still tell a run to leave
  ready to the operator, so a run will not use the ready pass until both change.
- A guarded session opens a draft, comments, replies and toggles ready with no
  stop from guard-mcp, attended or not.
- Accepted, unless the reach check says otherwise: injected text can post chosen
  content, as a comment or a draft's body, wherever the session's credentials
  and the server allow. A comment needs no branch, so any public issue accepts
  one if the server is not bound to the session's repositories.
- Accepted: the ready toggle passes on any PR the credentials reach, not only one
  the session opened, as title and body edits do since ADR 0034. A session could
  mark another PR ready, or back to draft.
- Accepted: each toggle to ready starts a billed review, bounded only by a
  project's own round limits.
- A new inline comment on a diff still stops at each of create, add and submit,
  because it goes through `pull_request_review_write`.
- `pr-body-check` runs on the shell route only, so an MCP create is checked by CI
  alone.
- A local session names the server `plugin_github_github`, so these passes reach
  it only under the name `github`.
- The base branch's matrix goes red on the pull request that makes this change,
  by design, and a human merges over it.
- Accepted residual, as in ADR 0034: the names and argument names are the
  vendor's. A listed argument reused for something else would pass. A new one
  asks.
