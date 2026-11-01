# 0052. guard-mcp passes draft creates, comments, replies, the ready toggle and issue writes by exact name

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

guard-mcp passes five writes, each by exact full name and only when every
argument is on that tool's list:

| Tool | Arguments that pass |
|---|---|
| `mcp__github__create_pull_request` | `owner`, `repo`, `title`, `body`, `head`, `base`, `draft` |
| `mcp__github__update_pull_request` | `owner`, `repo`, `pullNumber`, `title`, `body`, `reviewers`, `draft` |
| `mcp__github__add_issue_comment` | `owner`, `repo`, `issue_number`, `body` |
| `mcp__github__add_reply_to_pull_request_comment` | `owner`, `repo`, `pullNumber`, `commentId`, `body` |
| `mcp__github__issue_write`, `method` exactly `create` or `update` | `owner`, `repo`, `method`, `issue_number`, `title`, `body`, `labels`, `state`, `state_reason` |

The issue pass is for the Daily CI/CD Health Check routine, added at the
operator's request on 2026-11-01. The routine runs under the guards and keeps its
state in a weekly run-log issue: each week it closes the old issue and opens a new
one. With nobody present, the ask on those two writes ends as a denial, so the
routine would fail at its first rotation. On `issue_write`, another method, a
method that is not exactly one of those two strings, and `assignees`, `milestone`,
`type`, `duplicate_of`, `issue_fields`, the `parent_*` arguments or any other
argument all ask.

Anything else on those tools asks: closing a PR, a new base, the maintainer
flag, reviewers on a create, a reaction, an argument a later release adds,
arguments that are not an object, and a parser that does not finish. A non-draft
create is denied before these checks, as before.

The answer is the same in an interactive session and an autonomous run. guard-mcp
does not read the mode.

The routine templates list the five tools in `disallowed_tools`, so routines
rendered from them keep today's refusal. The Daily routine is not rendered from
them, so this does not reach it: its `disallowed_tools` is empty. Their prompts forbid marking a PR ready and writing outside the
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
- **The routine posts to one fixed log issue, with no rotation.** Rejected by the
  operator. It needs no wider guard, since the comment pass already covers it, but
  one issue grows without limit and the routine's weekly rollup goes.
- **Pass `issue_write` by name, or by method alone.** Rejected: `assignees` and
  `milestone` would pass, and a method a later release adds would pass unread.
- **A limit on target owners, from an operator-exported variable.** Rejected on
  the reach check below: the session's MCP route already refuses a repository that
  is not attached, so the limit would add configuration for nothing.

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

The issue rule, on 2026-11-01, on copies the same way. Each turned its named cases
red:

- The tool off its list (the rule removed): 12 cases, every pass and named reason.
- The method check removed: 18 cases, every unlisted method.
- The argument list ignored: 13 cases, every unlisted argument and the list pin.
- `assignees` added to the list: 3 cases.
- A `<tool>`-part match: 1 case, the other server.

On the shipped lists the matrix passes, with its jq-only and python3-only groups,
and pins the five argument lists. The base branch's matrix goes red on 11 cases, by design:
the ready toggle and draft create pins that used to ask (with their jq-only,
python3-only and planted-module versions), and the order check's line count.

**Reach, measured on 2026-11-01.** In a guarded cloud session with two attached
repositories, acting as a user with no connection to the target, the operator
approved one `add_issue_comment` to a public scratch repository that was not
attached. It was refused with "Access denied: repository ... is not configured
for this session", and nothing was written. One tool, one target and one session:
a create was not tried, and the refusal text is the only evidence of where it is
enforced.

`mcp__claude-code-remote__add_repo` widens that set. It asks under this guard, as
any unlisted write does, so it ends as a denial in an unattended run, and routines
refuse the whole server. The asking is what keeps the boundary in an unattended
run, so a pass on it would need its own decision.

**The permission layer, so far.** The Daily routine's run of 2026-11-01 lists only
built-in tools in `allowed_tools`. Its MCP reads raised no prompt, and each of its
comments raised guard-mcp's prompt and no other. That is evidence, not proof, that
a pass is enough there.

**Pending, to be recorded here before this is accepted:**

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
- Accepted: injected text can post chosen content, as a comment or a draft's
  body, on the repositories attached to the session, with no stop. The reach check
  shows the comment route stops there. If the platform drops that limit, these
  passes would reach any public issue, and this guard would not notice.
- Accepted: an AI session under the connected GitHub account can open, edit or
  close any issue it can reach, with no prompt. Reach is the session's attached
  repositories, on the check above, which covered a comment and not this tool. The
  routine's rule against touching issues it did not create is instruction text, and
  this guard does not hold it.
- Accepted: **`issue_write` reaches pull requests too.** The server's README
  describes it as "Create or update issue/pull request", and GitHub treats a PR as
  an issue here. A PR's number can be passed as `issue_number`, and the guard cannot
  tell the two apart from a number. So closing or reopening a PR, retitling it, and
  setting its labels pass on this route, while closing it through
  `update_pull_request` still asks. Labels include `plan:continue`, which starts a
  session, and `plan:blocked`; ADR 0018's check on who removed that label still
  applies. Read from the README, not tried. Merge, approval and resolving are not
  on this tool's list of methods and stay denied.
- Accepted: an issue write can trigger automation that reacts to issue events. In
  ControlTower only `claude.yml` does (`issues: opened`, `issue_comment: created`).
  Its job runs only for an author association of `OWNER` with a mention of the
  agent, so a session acting as a write-role user starts the workflow and the job
  skips, as the routine's own comments did on 2026-11-01. **If the connected GitHub
  account were the owner's, that gate would open**, and a session's issue or comment
  could start the agent with the repository's token. No other ControlTower workflow
  has an issue trigger. Other repositories were not checked.
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
