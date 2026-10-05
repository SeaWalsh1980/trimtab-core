# 0013. guard-mcp asks before MCP tools that may write

## Status

Accepted. Amended 2026-10-05: read verbs match in any case, and one Zoho
product namespace is stripped before the verb check. See
[Amendment (2026-10-05)](#amendment-2026-10-05).

## Context

MCP servers connected to a session expose tools that act on outside systems:
a store's catalogue, a mailing platform's campaigns, a tag manager's live
container. Before this record no guard matched them. Every guard's matcher
names built-in tools (`Bash`, `Read`, `Edit`, `Write` and the like), so an MCP
call ran under the permission rules alone, and the base ships no permission
rule for any MCP tool.

The operator wants each MCP call that may write to wait for their approval,
one call at a time, while reads run without friction.

The constraints:

- Claude Code names an MCP tool `mcp__<server>__<tool>`. The server part is
  set per deployment, often a generated ID, so the base cannot name servers.
  New servers and new tools appear without any change to this repository.
- Claude Code treats a hook that errors, times out or prints invalid JSON as
  making no decision. The guard must fail closed (ADR 0001) and finish well
  inside its 10 s timeout.
- When several hooks answer, the most restrictive answer wins: deny, then
  defer, then ask, then allow. A hook's `ask` shows the permission prompt even
  where an allow rule would have let the call through.
- Some servers put several verbs behind one tool. The tag manager's tools
  (`gtm_tag`, `gtm_version`, `gtm_workspace` and the rest) each take a
  required `action` argument (`get`, `list`, `create`, `update`, `remove`,
  `publish` and so on), so the tool's name cannot tell a read from a write.

The docs do not say what a hook's `ask` does outside an interactive session
in the default mode, so it was measured. A scratch hook answered `ask` for one
`echo` command that a settings allow rule explicitly permitted, so the hook
was the only thing that could stop it. Each run used Claude Code 2.1.289 on
2026-10-04, with `claude -p --output-format stream-json`:

| Mode | Result |
| --- | --- |
| `default` | Not run: the tool result was the hook's reason, flagged as an error, and the call was listed in the run's permission denials |
| `acceptEdits` | Not run, as above |
| `auto` | Not run, as above |
| `dontAsk` | Not run, as above |
| `bypassPermissions` | Not run, as above |
| `bypassPermissions`, interactive (`--dangerously-skip-permissions`) | The permission prompt appeared; declining it stopped the call |

Two pitfalls were found in measuring:

- Run with the Haiku model, `--permission-mode auto` silently started in
  `default`. The hook's log showed the mode it was given, and the `auto` row
  above is from a rerun with the default model, which did start in `auto`.
- The model's one-line summary twice said the command ran when the tool
  result showed it had not. A test of this guard checks its output, never a
  model's account of it.

Not measured: an interactive session in `auto`, `acceptEdits` or `dontAsk`.

## Decision

Add `hooks/guard-mcp.sh`, a PreToolUse hook on `mcp__.*`, registered in
`settings.base.json` and included in the session-start liveness check
(ADR 0004) and the bootstrap self-probe.

- **It classifies by tool name only.** It parses `tool_name` and splits off
  the tool after `mcp__<server>__`. It never reads the call's arguments or the
  server's description of a tool.
- **A tool whose name starts with a read verb passes:** `get_`, `list_`,
  `search_`, `query_` or `read_`, or the same verbs with a hyphen. Matching is
  case-sensitive. Passing means exiting 0 with no output, which is no decision:
  the session's own permission rules still apply.
- **Every `gtm_*` tool asks, reads included**, because its verb is in an
  argument, not its name.
- **Everything else asks**, answering
  `permissionDecision: "ask"` with a reason that names the tool and nothing
  from its arguments. That includes an unknown verb, a name that is not of the
  `mcp__<server>__<tool>` shape or that splits ambiguously, a non-MCP name, an
  empty or unparseable payload, and a missing parser (neither `jq` nor
  `python3`, ADR 0001).
- **It is lifted only by the operator**, by exporting `HOOK_ALLOW_MCP=1`
  before launching Claude Code. That is the route for a reviewed unattended
  job that needs MCP writes.

Every failure answers `ask`, not a block, because `ask` already satisfies
ADR 0001: no call proceeds without a person approving it, and in every
unattended mode measured an `ask` ends as a denial.

## Options considered

- **Permission ask rules per tool in settings.** Rejected. A rule names one
  tool, so a new tool or server is uncovered until someone adds one, which
  fails open. The server part of the name differs per deployment, so the base
  could not ship the rules at all.
- **Read the tag manager's `action` argument against a list of read actions**
  (`get`, `list`, `live`, `getStatus`), asking for anything else. Rejected by
  the operator. It would spare a prompt on every tag manager read, at the cost
  of parsing arguments and of a list that could be wrong for a server. The
  operator accepted the prompts instead.
- **Block writes outright (exit 2).** Rejected. The operator wants to approve
  a write call by call, not to lose the tools.
- **Ask on every MCP call.** Rejected. Prompts on every read train the
  operator to approve without reading, which weakens the prompt that matters.
- **A data file of read verbs, in the manner of the `hooks/secrets.d` packs
  (ADR 0008).** Rejected. Those packs only ever add blocks. A verb file only
  ever adds passes, so an instance could widen the control with data and no
  change to the guard.

## Consequences

- Every tag manager call prompts in an interactive session, reads included.
- An unattended run (`claude -p`, a routine) cannot use any tag manager tool
  or any MCP tool outside the read verbs, because an `ask` ends as a denial
  there, unless the operator exports `HOOK_ALLOW_MCP=1` for that run.
- Accepted residual: classification trusts the server's naming. A tool with a
  side effect behind a read-verb name passes this guard, and only the
  session's permission rules stand between it and a call.
- A server name containing `__` makes the split ambiguous. Such a call asks.
- The liveness check and the bootstrap self-probe now cover five guards.
  ADR 0004 records four; it is not rewritten.
- The CI step that runs the base branch's guard tests against a pull request
  fails on the pull request that adds this guard, because the base's liveness
  fixture stubs four guards. That step is meant to go red on a legitimate
  change to the base's expectations, for a person to merge over.

## Amendment (2026-10-05)

### Status

Accepted. Amends the Decision's read-verb clause ("Matching is
case-sensitive"); the rest of the Decision stands.

### Context

The Zoho connector names its tools with a product namespace and mixed case:
`ZohoBooks_list_invoices`, `ZohoBooks_get_bill`, `ZohoInventory_list_items`,
`ZohoWorkdrive_Get_File_Preview`, `ZohoWorkdrive_Search_Records`. The guard
passed a tool only if its name started with a lower-case read verb, so every
Zoho read asked. Confirmed against the guard as pinned on 2026-10-05. That is
the outcome the Options above rejected for "ask on every MCP call": prompts on
every read train the operator to approve without reading.

### Decision

- **The `gtm_*` check runs first, unchanged.** Every `gtm_*` tool still asks.
- **One namespace is stripped, once.** If the tool name starts with exactly
  `ZohoBooks_`, `ZohoInventory_` or `ZohoWorkdrive_` (case-sensitive), that
  one segment is removed. Nothing else is stripped, and a second namespace is
  not.
- **Read verbs match in any case.** What remains passes if it starts with
  `get`, `list`, `search`, `query` or `read` in any case, followed by `_` or
  `-`. The case-insensitivity is written as bracket patterns
  (`[Gg][Ee][Tt][_-]*`), the idiom `guard-bash.sh` already uses, not with
  `shopt -s nocasematch` (which would apply to the rest of the script) or
  `${x,,}` (which kills the hook on bash 3 with no output, and no output
  fails open).
- **Everything else asks, as before.** Every fail-closed path is unchanged.

The three namespaces are the connector's public product names. They are
mechanism, not instance data, on the same footing as `gtm_`.

### Options considered

- **Skip any CamelCase prefix before the verb.** Rejected. It lets
  `Delete_List_Items` and `BulkDelete_list_x` through: the skipped segment can
  itself be the write verb.
- **An instance-side file of namespaces to strip.** Rejected for the reason
  the verb data file was rejected above: a file that only ever adds passes
  lets an instance widen the control with data and no change to the guard.
- **Add `fetch` to the read verbs.** Rejected. `fetch_url`-style tools make
  outbound requests on arguments the guard never reads, which makes them an
  exfiltration channel, not a read.

### Consequences

- Zoho Books, Inventory and WorkDrive reads named with a read verb pass
  without a prompt.
- The case widening applies to every server, not only Zoho: `Get_item`,
  `GET_item` or `Read-x` on any server now passes. `GETAWAY` and `getaway`
  still ask, because no `_` or `-` follows the verb.
- Accepted residual: `ZohoWorkdrive_Fetch_Files_Folders`,
  `ZohoWorkdrive_My_Folder_Files`, `ZohoWorkdrive_My_Folder_Links` and
  `ZohoWorkdrive_My_Trashed_Files` keep asking. They are reads, but their
  names carry no read verb, or carry `fetch`.
- The residual of the original Decision still holds, now over more names: a
  tool with a side effect behind a read-verb name passes this guard.
- A new namespace from this or any other connector needs a change to the
  guard, its tests and this record. It does not get a data file.
- The CI step that runs the base branch's guard matrix against the pull
  request's hooks fails on the pull request that makes this change. The
  base's matrix expects `Get_item` to ask. That step is meant to go red on a
  legitimate change to the base's expectations, for a person to merge over.
