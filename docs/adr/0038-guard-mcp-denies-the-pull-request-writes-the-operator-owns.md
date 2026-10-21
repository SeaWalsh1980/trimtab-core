# 0038. guard-mcp denies the pull-request writes the operator owns

## Status

Accepted on merge. Amends ADR 0013
(`docs/adr/0013-guard-mcp-asks-before-mcp-tools-that-may-write.md`): four
kinds of MCP call that it asked about are now denied, and the deny runs before
every pass that ADRs 0027, 0028 and 0034 added. The rest of those ADRs stands.
No trial is pending: the guard matrix exercises the decision in full.

The base branch's matrix holds against this change except for one case, which
pinned an MCP merge as an ask; it is now a deny. A project that runs the base
branch's matrix against a pull request's hooks sees that one case red on this
pull request, by design.

## Context

ADR 0037 made guard-github refuse, in a shell, the GitHub writes a consuming
project's instruction file reserves for a person: a pull request opened as
anything but a draft, an approving review as the agent's account, and
resolving a review thread (only its author may). It left merging to a
project's own merge gate and the server's ruleset, because `gh pr merge` is
the route that gate checks. Its residuals named MCP tools as guard-mcp's.

GitHub's MCP server reaches the same writes by name:

- `merge_pull_request` merges.
- `create_pull_request` opens a pull request, as a draft only when its `draft`
  argument is true.
- `pull_request_review_write`, and in older releases
  `create_pull_request_review`, `create_and_submit_pull_request_review` and
  `submit_pending_pull_request_review`, submit a review whose `event` may be
  `APPROVE`. A `method` argument selects the operation in the newer tool.
- A tool that resolves a review thread names it (`resolve_review_thread`), or
  is a review tool's method.

Before this change guard-mcp asked about each of them, as about every write it
cannot read (ADR 0013). An ask puts the call in front of whoever is at the
prompt. In an attended session that is the operator approving a merge or an
approval out of context, one prompt among many; in an unattended one, the ask
ends as a denial anyway (ADR 0013). For these four the rule's point is that no
agent-driven call does them, so the prompt adds no judgement the rule wants.

## Decision

guard-mcp denies, before any other check that can answer:

1. **Resolving a review thread, by name.** The last `__` part of the name holds
   one of thread, review, comment or conversation and says resolve, in any
   case, once every "unresolve" is removed. Reopening a thread is not resolving
   it, so `unresolve_review_thread` still asks.
2. **A merge.** The last `__` part is exactly `merge_pull_request`, whatever its
   arguments.
3. **A non-draft create.** The last `__` part is exactly `create_pull_request`
   and its `draft` argument is not the JSON value `true`: absent, `false`,
   `null`, the string `"true"` and `1` all deny. A draft create falls through
   and asks, as before.
4. **An approval or a resolving review method.** The last `__` part is one of
   the four review tools above, and its `event`, with white space removed and
   in any case, is `APPROVE`, or its `method`, once "unresolve" is removed, says
   resolve. A comment or a request for changes falls through and asks.

Fail closed. Arguments that are not an object, an `event` or `method` that is
not a string (null included), and a parser that does not finish all deny, with
a reason that says the arguments could not be read. A verdict counts only from
a parser that exits 0, as for the PR edit (ADR 0034).

Matching is by the last `__` part, on any server: a server's name is set per
deployment, the same tool is installed under several names, and the cost of
denying a same-named tool on another server is a refusal, never a pass. It is
exact and case-sensitive for the three lists (`GH_MERGE`, `GH_CREATE`,
`GH_REVIEW`), which the matrix pins as it pins the pass lists, and by word for
resolving.

The arguments are read inside jq, or `python3 -I` without jq, and only a
verdict word comes back; no argument reaches a reason. The resolve-by-name
match uses bash patterns only, so a stub PATH cannot empty it.

`HOOK_ALLOW_MCP`, exported by the operator, still lifts the whole guard.

## Options considered

- **Keep asking (ADR 0013 as it stood).** Rejected: the operator would approve,
  at a prompt, the agent's own merge or approval, which is what the rule
  forbids; the prompt is not the person's review the rule asks for.
- **Deny by full name only (`mcp__github__…`).** Rejected: the same server is
  installed under other names (a plugin's server is
  `plugin_<plugin>_github`), so a full-name list misses it. ADR 0034 rejected
  a `<tool>`-part *pass* for the opposite reason; a deny errs the safe way.
- **Allow the MCP merge, as guard-github allows `gh pr merge`.** Rejected: a
  project's merge gate is written against the shell route it can read, and
  none reads MCP calls. A merge through MCP would skip it. A project that wants
  MCP merges can extend its own gate and propose lifting this.
- **Deny any review tool call that mentions APPROVE anywhere.** Rejected: a
  comment's body may say "approve", and denying it would block an ordinary
  review reply. The check reads the `event` field the server acts on.
- **Treat an unreadable argument as an ask.** Rejected: an ask is a prompt
  the operator may approve without reading; for these writes an argument the
  guard cannot read denies, as the shell guard refuses.

## Consequences

- An MCP merge, approval or thread resolution is never made by an agent
  session, attended or not, without the operator exporting `HOOK_ALLOW_MCP`.
- A pull request opened through MCP must pass `draft: true`; the reason says
  so, so the agent can call again.
- Measured on this change: the matrix holds 1,303 cases (1,231 before), all
  passing; each new deny case fails against the guard before it.
- The base branch's matrix shows one red case on this change, the merge that
  used to ask (see Status).
- `mcp__ccd_pr__set_auto_merge`, the desktop app's auto-merge switch, still
  asks. It is not GitHub's merge tool and was outside this change's scope;
  whether it should deny too is left open.

### Residuals

- **A guard that cannot parse the payload asks, not denies.** With neither jq
  nor python3, guard-mcp cannot read the tool's name, so a merge asks
  (ADR 0001).
- **Other names.** A tool that merges, approves or resolves under a name not
  covered here (another server's own vocabulary, a generic GraphQL tool) is
  asked about by ADR 0013's rule, not denied.
- **Server-side state.** A pending review created as a comment and then
  submitted as APPROVE through another route is that route's to catch.

The server's ruleset and code owners stay the boundary; this is a second
layer, as guard-github is.
