# 0027. guard-mcp passes the desktop app's UI and read-only tools by exact name

## Status

Accepted (2026-10-16). Amends ADR 0013; the rest of 0013 stands.

## Context

The Claude desktop app injects its own MCP tools into a session, on servers
whose names it fixes: `ccd_turn`, `ccd_view`, `ccd_window`, `ccd_pr`,
`ccd_session_mgmt`, `ccd_host`, `ccd_session`, `ccd_connectors`, `ccd_settings`,
`ccd_directory`, `ccd_sidebar`. Several of these tools carry no read verb in
their names (`mcp__ccd_turn__update_plan`, `mcp__ccd_turn__wrap_up`,
`mcp__ccd_view__show_pane`), so guard-mcp asks on them, and the app calls some
of them on every turn. An operator-approved plan cannot run unattended, and a
prompt that appears every turn trains the operator to approve without reading,
the outcome ADR 0013 rejected "ask on every MCP call" for.

The same servers also carry tools that must keep asking. `ccd_pr` can turn on
auto-merge; `ccd_session_mgmt` can change a session's permission mode, send it
a message, or delete, clear, stop, archive or detach it; `ccd_host` can sync a
branch and delete worktrees; `ccd_settings` can change a setting. Each of those
servers mixes tools that are safe to pass with tools that are not.

ADR 0013 rejected per-tool permission rules because a server's name is set per
deployment, so the base could not name it. These server names are fixed by the
app, not by a deployment, so naming them is mechanism, on the same footing as
`gtm_` and the Zoho namespaces.

## Decision

guard-mcp checks the full tool name against a list of exact names, after the
name-shape check and before the existing classification. A name on the list
passes (exit 0, no output). Any other name, including an unknown or new `ccd_*`
tool, falls through to ADR 0013's classification unchanged. The comparison is
a literal string equality, never a prefix, glob or pattern.

The list:

- `mcp__ccd_turn__update_plan`, `mcp__ccd_turn__wrap_up`
- `mcp__ccd_view__show_pane`, `mcp__ccd_view__close_pane`,
  `mcp__ccd_view__get_layout`, `mcp__ccd_view__set_transcript_view`
- `mcp__ccd_window__get_window_layout`, `mcp__ccd_window__open_session_in`,
  `mcp__ccd_window__close_split`, `mcp__ccd_window__set_sidebar_collapsed`
- `mcp__ccd_pr__get_status`, `mcp__ccd_pr__bind_pr`, `mcp__ccd_pr__unbind_pr`,
  `mcp__ccd_pr__set_monitor`
- `mcp__ccd_session_mgmt__get_session`, `mcp__ccd_session_mgmt__list_sessions`,
  `mcp__ccd_session_mgmt__list_events`, `mcp__ccd_session_mgmt__get_usage`,
  `mcp__ccd_session_mgmt__search_session_transcripts`,
  `mcp__ccd_session_mgmt__set_session_title`
- `mcp__ccd_host__get_storage_usage`, `mcp__ccd_host__open_in_editor`,
  `mcp__ccd_host__reveal_path`, `mcp__ccd_host__request_keep_awake`
- `mcp__ccd_session__list_start_targets`, `mcp__ccd_session__dismiss_task`
- `mcp__ccd_connectors__session_connectors_status`

Some of these already passed as reads by name; they are listed anyway so the
list is the whole statement of what the app may do unprompted.

The session self-tuning tools, `mcp__ccd_session_mgmt__set_session_model`,
`set_session_effort` and `set_session_fast_mode`, are deliberately off the
list, so they ask. A flat deny was considered and rejected by the operator: a
session may propose such a change, with approval.

guard-mcp stays never-deny. Nothing in this change exits 2.

## Options considered

- **A prefix allowlist, `mcp__ccd_*`.** Rejected. It passes auto-merge,
  permission-mode escalation and session deletion with the UI tools.
- **A per-server allowlist** (pass every tool on `ccd_turn`, `ccd_view`, and
  so on). Rejected. `ccd_pr`, `ccd_host` and `ccd_session_mgmt` each mix safe
  and dangerous tools, so any server-level pass on them passes a dangerous
  tool, and a server-level pass on the others passes whatever the app adds
  there next.
- **Leave it as it is.** Rejected. A prompt on every turn blocks unattended
  execution of an approved plan and trains rubber-stamp approval.
- **Deny the self-tuning tools.** Rejected by the operator: they should ask.
  A deny would also not belong in guard-mcp, which never exits 2 (ADR 0013).
- **A data file of allowed names.** Rejected for the reason ADR 0013 rejected
  a verb file: a file that only ever adds passes lets an instance widen the
  control with data and no change to the guard. The list lives in the guard,
  and changing it takes a change to the guard, its tests and this record.

## Trial

Each negative control was checked against a prefix-widened list: the exact
comparison was replaced locally with a match on `mcp__ccd_*`, the guard matrix
was run, and the change was reverted. The result is recorded in the pull
request that introduces this record.

## Consequences

- The listed app tools run without a prompt, in every mode, unattended runs
  included.
- Every other app tool keeps asking, and so does any app tool added later,
  unless its name is a read verb by ADR 0013's rule.
- Accepted residual: the tool names are owned by the app's vendor. A listed
  name reused for a tool with a side effect would pass. A renamed tool falls
  back to the classification and asks, which fails closed.
- `mcp__ccd_window__open_session_in`, `mcp__ccd_host__open_in_editor`,
  `mcp__ccd_host__reveal_path` and `mcp__ccd_host__request_keep_awake` act on
  the local desktop, not on an outside system, and were accepted on that basis.
- The bootstrap self-probe also asks guard-mcp about an app tool off the list
  (`mcp__ccd_pr__set_auto_merge`), so a list widened to a prefix fails the
  install, and it notes, without failing, an allowlisted tool that no longer
  passes.
- Both CI steps that run the base branch's guard matrix against the pull
  request's hooks may go red on the pull request that makes this change, if
  the base's matrix expects any listed name to ask. Those steps are meant to
  go red on a legitimate change to the base's expectations, for a person to
  merge over.
