# 0020. Routines refuse the remote-session MCP tools

## Status

Accepted for the refusal (2026-10-12): a routine updated from these templates
read back with the tools listed, and its run made no call to them. The
consequence that no PR event reaches a run after its work did not hold; see
"Trial result" below, where what follows from that is left open.

2026-10-16: for desktop sessions, ADR 0027
(`docs/adr/0027-guard-mcp-passes-desktop-app-tools-by-exact-name.md`) lets
guard-mcp pass the app's `mcp__ccd_pr__set_monitor`, so PR events can reach
a desktop session after its work. This record still governs cloud routines.

2026-10-17: ADR 0028
(`docs/adr/0028-guard-mcp-passes-set-monitor-only-for-prs-the-session-opened.md`,
proposed) narrows that desktop pass to PRs the session created.

2026-10-17: ADR 0031
(`docs/adr/0031-guard-mcp-passes-pr-workflow-writes-by-exact-name.md`) lets
guard-mcp pass `mcp__claude-code-remote__subscribe_pr_activity` outside
routines. The templates still refuse it, so this record still governs
routines.

Was: proposed. Becomes accepted once a routine updated from these templates
reads back with the tools listed in its session config, and one of its runs
shows no call to them (see "Trial, still open" below).

## Context

A cloud routine session carries the session's own MCP server,
`claude-code-remote`, whatever the routine's `mcp_connections` say. After
opening its draft PR, a guarded per-project routine called that server's
`subscribe_pr_activity`. guard-mcp (ADR 0013) asks before any MCP tool whose
name is not a read verb, so the unattended run waited for an operator who was
not there.

Approving it would have been worse. A subscription delivers the PR's comments,
reviews and CI events into the session as they happen. Those are untrusted
text, arriving after the method's work is done, in a session that still holds
write access to a repository. Nothing in a routine's method needs them: it
ends with its report in the final message.

The routines API takes the session's tool lists in the routine body. A routine
record, read back, shows each template's
`job_config.ccr.session_context.allowed_tools` mirrored to
`session_request.config.allowed_tools`, and a
`session_request.config.disallowed_tools` beside it, empty on every routine
read. No record yet sets a refusal list in `session_context`, so the mirror
for it is expected, not observed.

## Decision

- **Each routine template refuses the remote-session MCP tools** in
  `job_config.ccr.session_context.disallowed_tools`, beside `allowed_tools`:
  the server as a whole (`mcp__claude-code-remote`) and, by name, the tool
  that stalled a run (`mcp__claude-code-remote__subscribe_pr_activity`), in
  case the server-wide form is not honoured.
- **It holds for the conformance, retro and upstream-retro templates alike**,
  and a test pins it in each render.
- **A routine created or updated from a render is read back** before it is run,
  and `session_request.config.disallowed_tools` must list both entries. If it
  does not, the field is not honoured where it is written: stop, and do not
  enable the routine.

## Options considered

- **A prompt instruction not to subscribe.** Rejected. It is text the model
  may not follow, and what must hold whatever the model decides is enforced
  by configuration, a hook or a test, not by instruction alone. The run that
  stalled had a prompt that said nothing either way; an instruction would
  have been the only thing between it and the call.
- **guard-mcp treats the subscription as a read and lets it pass.** Rejected.
  It is a guard change, with the review that carries, for a tool no routine
  needs. It removes the stall but keeps the untrusted wake-ups, the part that
  matters, and it would apply to interactive sessions too.
- **`HOOK_ALLOW_MCP` exported in the routine's environment.** Rejected. It
  lifts guard-mcp for every MCP call in the run, and the operator's override
  is not something a routine sets for itself.

## Consequences

- An unattended run no longer waits on that prompt, and no PR event reaches
  the session after the method's work.
- A routine cannot watch its own PR. None does: each reports in its final
  message, and the operator reads the PR.
- If the platform adds tools to that server, the server-wide entry covers
  them, provided it is honoured; the named entry covers only the one seen.
- guard-mcp is unchanged, and still asks for any other MCP tool that may
  write.

## Trial, still open

The field's location rests on the read-back shape above, not on a routine
that sets it. The first update of a routine from a render on this base reads
it back; the result is recorded here, and if the list is empty, this record
is revisited before any rejected option is.

## Trial result (2026-10-12)

Partly held. A per-project routine updated from a render read back with both
entries in `session_request.config.disallowed_tools`, and its manual run made
no call to the server's tools. But the run, after publishing a draft PR and
ending with its report, was resumed once by a notification for that PR: the
platform, not the refused tool, delivered it. The resumed turn read the PR's
check runs and ended. So the decision stops the stall it was taken for, and
the consequence "no PR event reaches the session after the method's work"
does not hold for a run that publishes a PR. The untrusted wake-up this record
meant to keep out is not fully kept out; what reaches the session is limited
to the PR the run itself opened. Not yet decided: whether that needs a further
control, or is accepted.
