# 0027. guard-mcp passes the desktop app's UI, read-only and PR-monitoring tools by exact name

## Status

Accepted (2026-10-16), by the operator. Amends ADR 0013; the rest of 0013
stands. Departs from ADR 0020 for desktop sessions only; see "PR
monitoring, and ADR 0020".

2026-10-17: ADR 0028
(`docs/adr/0028-guard-mcp-passes-set-monitor-only-for-prs-the-session-opened.md`,
proposed) takes `mcp__ccd_pr__set_monitor` off this list and passes it only
for PRs the session created, denying it otherwise. That narrows the accepted
risk below; the rest of this record stands.

## Context

The Claude desktop app injects its own MCP tools into a session, on servers
whose names it fixes: `ccd_turn`, `ccd_view`, `ccd_window`, `ccd_pr`,
`ccd_session_mgmt`, `ccd_host`, `ccd_session`, `ccd_connectors`, `ccd_settings`,
`ccd_directory`, `ccd_sidebar`. Several of these tools carry no read verb in
their names (`mcp__ccd_turn__wrap_up`, `mcp__ccd_view__show_pane`), so
guard-mcp asks on them, and the app calls some
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

- `mcp__ccd_turn__wrap_up`
- `mcp__ccd_view__show_pane`, `mcp__ccd_view__close_pane`,
  `mcp__ccd_view__get_layout`, `mcp__ccd_view__set_transcript_view`
- `mcp__ccd_window__get_window_layout`, `mcp__ccd_window__open_session_in`,
  `mcp__ccd_window__close_split`, `mcp__ccd_window__set_sidebar_collapsed`
- `mcp__ccd_pr__get_status`, `mcp__ccd_pr__unbind_pr`,
  `mcp__ccd_pr__set_monitor`
- `mcp__ccd_session_mgmt__get_session`, `mcp__ccd_session_mgmt__list_sessions`,
  `mcp__ccd_session_mgmt__list_events`, `mcp__ccd_session_mgmt__get_usage`,
  `mcp__ccd_session_mgmt__search_session_transcripts`
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
`mcp__ccd_session_mgmt__set_session_title` is off the list too: it renames any
session by its id, not only the caller, so it asks.
`mcp__ccd_turn__update_plan` is off the list by the operator's choice: a plan
change is the operator's to approve. `mcp__ccd_turn__wrap_up` stays on it,
since its prompt came on most turns.

The list applies only where the desktop app supplies its tools. Not every app
build or mode exposes every listed name: on 2026-10-16 one desktop session
showed no `ccd_turn` server and no `mcp__ccd_session__list_start_targets`,
and did show `mcp__ccd_session__mark_chapter`, which is not listed and asks.
A listed name that no tool carries passes nothing. Cloud routines carry none
of these names.

guard-mcp stays never-deny. Nothing in this change exits 2.

### PR monitoring, and ADR 0020

`mcp__ccd_pr__set_monitor` is on the list on purpose. It switches the app's
Auto-fix on or off for the session's bound PR: with it on, the app wakes the
session on CI failures, merge conflicts and review comments, and the session
may fix, commit and push to that PR without asking. The operator wants
unattended desktop sessions to watch their own PRs, and an ask with no one
present ends as a denial, so the tool has to pass for that to work. guard-mcp
reads names, not arguments (ADR 0013), so it cannot pass "off" and ask on
"on"; the tool passes whole.

guard-mcp's pass is necessary, not sufficient. The app asks the user to
approve each `set_monitor` call itself, except in auto mode, where it may
approve without asking, and in bypass-permissions mode, where it does not
ask. An unattended session that is to watch its own PRs therefore also has
to run in one of those modes; in the default mode the app's prompt waits,
and Auto-fix does not turn on.

This departs from ADR 0020 for desktop sessions. ADR 0020 rejected letting
guard-mcp pass a cloud routine's PR-activity subscription, because the events
it delivers are untrusted text arriving in a session that can still write.
The same holds here, and it is accepted here because the operator chose
unattended watching for desktop sessions, where a routine has no use for it.
ADR 0020 still governs cloud routines, which do not carry the app's tools.

`mcp__ccd_pr__bind_pr` stays off the list, so a session cannot attach itself
to a PR of its choosing without a prompt. That does not confine
`set_monitor` to PRs the session created: the app binds some PRs to a
session by itself, from links and calls in the session. The accepted risk is
therefore Auto-fix on any PR the app has bound to the session. Narrowing it
to PRs the session opened is tracked in issue #69.

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
- **`set_monitor` asks, as ADR 0020 would have it.** Rejected by the
  operator. Auto-fix would then need the operator's click or approval on
  every PR, and an unattended session could not watch its own PRs.
- **`bind_pr` passes as well.** Rejected. It binds the session to any PR by
  URL, and restores one the operator dismissed; with `set_monitor` passing,
  a session steered by text in a PR or issue could attach itself to another
  PR and switch Auto-fix on for it with no prompt.

## Trial

Each negative control was checked against a prefix-widened list, on
2026-10-16: the exact comparison was replaced locally with a match on
`mcp__ccd_*`, the guard matrix was run, and the change was reverted.

- With the exact list: 456 pass, 0 fail.
- Widened to the prefix: 10 cases failed. They were every off-list app tool
  (`set_auto_merge`, `set_session_permission_mode`, `sync_with_base_branch`,
  `send_message`, the three self-tuning tools, and a made-up
  `mcp__ccd_turn__evil`) and both suffix cases (`update_plan_x`,
  `update_planX`).
- `mcp__xccd_turn__update_plan` still asked, because the `mcp__ccd_` prefix
  does not match it. That case guards against a looser widening, a substring
  or `*ccd_*` match, not against this one.

2026-10-16, after review: the list lost `bind_pr`, `set_session_title` and
`update_plan`, and the tests changed with it. The pass names live in one
array, and a new case fails when the guard's `CCD_ALLOW` and that array
differ; the exact-match cases (a suffix, a run-on suffix, a server prefix)
moved to `wrap_up`. On the shipped list the matrix passes, 457 cases, 0
failures. The prefix-widening trial above was not re-run; its off-list and
suffix cases still ask on the shipped list.

## Consequences

- The listed app tools run without a prompt from guard-mcp, in every mode,
  unattended runs included. Where the app asks for a tool itself
  (`set_monitor`), that prompt still applies outside auto and
  bypass-permissions modes.
- Every other app tool keeps asking, and so does any app tool added later,
  unless its name is a read verb by ADR 0013's rule.
- Accepted: a session can switch Auto-fix on, with no prompt, for any PR the
  app has bound to it, and then receives that PR's comments and CI output,
  untrusted text, while it can push to the PR. Binding a PR itself still asks.
- Accepted: `mcp__ccd_session_mgmt__search_session_transcripts` and
  `mcp__ccd_session_mgmt__list_events` return other sessions' transcript
  text without a prompt, and a transcript can hold a credential an operator
  pasted into it. ADR 0013's secret-word rule reads tool names, not results,
  so it does not see this. Both passed before this record as read verbs; the
  list states it rather than changes it.
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
