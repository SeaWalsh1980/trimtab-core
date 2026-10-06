# 0010. Count session events, never read session content

## Status

Proposed.

This record adds to ADR 0005 and leaves it unchanged. ADR 0005 rejected
mining session transcripts and gave one reason. This record gives the
stronger reasons, and admits a narrower kind of session evidence.

## Context

ADR 0005 rejected mining session transcripts for feedback because
"transcripts hold the operator's own words and are not the loop's to read".
That is a reason about consent, and an operator can consent for themselves,
so it is the weakest reason available. The option came back for two
reasons.

First, a widely used open-source Claude Code harness learns from sessions
by default. It was reviewed on 2026-10-04, at its release of 2026-10-01.

- In its default hook profile, a hook writes the input and output of every
  tool call, MCP tools included, to a file on the machine.
- Before writing, it redacts a value only when the value follows a key name
  such as `token` or `password`. Three things pass through: a bare token, a
  database URL with a password in it, and the private key in a
  service-account file. Personal data, such as the customer records a
  commerce connector returns, is not handled at all.
- A background model reads that file and writes "instincts". Each instinct
  has a confidence score that rises when a pattern repeats. An instinct at
  0.7 or above counts as approved. By default, up to six relevant instincts
  are injected into each new session, and no person approves them.

Second, evidence from pull requests alone has blind spots.

- Only merged pull requests count. An abandoned approach leaves nothing.
  Neither does work that never becomes a pull request, such as operations,
  investigations and actions taken through MCP connectors.
- The block is written when the work ends, often after the context has been
  compacted, so friction from early in a session goes unreported.
- A proposal needs feedback from 2 or more pull requests on one ID
  (ADR 0005), within the eight-week window (ADR 0006). In a repository with
  few pull requests, evidence can decay before it counts.
- Guard denials are recorded nowhere the loop can read. On 2026-10-04,
  `guard-bash` blocked a read-only `gh api … | grep`, because the grep
  pattern contained a recursive-delete keyword. That is the quoting
  limitation documented in the guard's header. The denial exists only in
  that session's transcript.

The loop already takes two kinds of evidence from sessions. Both stay on
the machine and both are gated.

- The rule-usage observer (`hooks/rule_usage.py`) records which instruction
  files, skills, subagents and slash commands a session reaches. It records
  them as names, paths, byte counts and timestamps. By design it records no
  prompt text, arguments or tool input.
- `/trimtab-retro --memories` drafts feedback from the operator's feedback
  memories, in the agent's own words.

Both reach GitHub only as drafts that the operator chooses to file. That
principle has never been written down, so each new source would have to
argue it again.

The constraints:

- Nothing changes the harness without review, and every write to GitHub
  sits behind a dry run and a confirmation (ADR 0005).
- A hook runs on every tool call. A guard must fail closed (ADR 0001), and
  an observer must never block.
- A session never edits the live control plane directly. Changes reach
  it through a pull request (ADR 0002).
- No new infrastructure unless it is needed.

## Decision

**The loop may count what happens in a session. It never reads what was
said in one. Nothing it counts changes the harness except through a pull
request.**

**Session content is never evidence.** The loop does not read or copy
prompts, command lines, file contents, tool input or output, or
transcripts. It does not have a model summarise them either. The reasons,
strongest first:

1. **Injection would persist.** Sessions read untrusted text: web pages,
   issues, other people's repositories. A learner that turns what a session
   did into instructions for later sessions lets an injection outlive the
   session that read it.
2. **Nobody would approve the change.** An instruction learned from a
   session takes effect without review. ADR 0005 puts that step behind a
   gate.
3. **Repetition is not correctness.** Confidence that grows with repetition
   reinforces whatever the agent keeps doing, including a mistake.
4. **Friction is not always a defect.** A guard or a PROHIBIT that blocks
   is usually doing its job. A loop that learns from friction wears down
   the REQUIRE and PROHIBIT items, which ADR 0005 does not let even a
   project override.
5. **What it learns has no ID.** A learned instruction cannot be cited,
   tallied or overridden. It becomes a second body of rules, with no
   precedence over the first.
6. **Tool output carries secrets and personal data.** Redaction by pattern
   misses shapes it does not know, and personal data has no fixed shape.
7. **A model's judgement cannot be replayed.** The tally is deterministic,
   because ADR 0006 rebuilds it from labelled pull requests. A model in the
   counting path would count the same evidence differently from run to run.

The operator's privacy, the reason ADR 0005 gave, still holds. It is just
not the only reason.

**Session events are evidence.** An event is a structured record with no
content. It may hold:

- the kind of event;
- the harness item, guard, skill or command it concerns, by name or ID;
- a reason code;
- the session ID;
- the project's ID prefix, from its lock;
- counts, byte sizes and timestamps;
- paths, but only of harness files.

An event never holds anything a person typed, a command line, file
contents, tool input or output, or the path of a file the work touched.
The rule-usage records are events. So is a guard denial, recorded by guard
and reason code.

**Events stay on the machine that recorded them.** Each kind of event is
stored the way the rule-usage log is:

- an append-only file under the Claude config directory's `logs/`;
- created with mode 0600;
- rotated by size;
- read through a window no longer than ADR 0006's eight weeks.

A hook never sends an event over the network.

**Events never gate anything.** They reach GitHub the way local evidence
already does. The local retrospective (`/trimtab-retro --usage`) turns them
into draft feedback entries, and the operator files or discards each one.

Event counts may also be shown to the agent while it writes a pull request
block, to remind it of friction lost to compaction. What it reports is
still its own entry, and the entry is reviewed in the pull request. The
block itself does not change. A later change that adds an events section
to the block follows the versioning rule in ADR 0007.

## Options considered

- **Learn from transcripts with a model, as the reviewed harness does.**
  Rejected, for reasons 1 to 7.
- **Mine transcripts, but only to draft proposals for review.** Rejected.
  A gate answers reason 2 but not reasons 1, 6 or 7. The loop would still
  read the content, secrets and personal data would still enter it, and
  injected text could still steer what gets proposed.
- **Redact transcripts, then mine them.** Rejected. The reviewed harness
  shows how redaction by pattern fails: bare tokens and private keys get
  through, and personal data has no pattern.
- **Keep pull requests as the only evidence.** Rejected. Abandoned work,
  work outside pull requests, and forgotten friction would stay invisible.
  Friction at the guards could not be counted at all.
- **Send events to GitHub from the hook.** Rejected. A network call on
  every tool call adds latency and a failure mode. It needs a credential in
  the hook, and it writes to GitHub without the dry run and confirmation
  that ADR 0005 requires.
- **Commit events to the instance repository.** Rejected. Every session
  would write into the checkout that the live harness loads from
  (ADR 0002). Every commit pushed from it would be a write to GitHub that
  needs a gate (ADR 0005).
- **Publish weekly counts to GitHub automatically, so cloud routines can
  see them.** Rejected for now. An unattended timer cannot answer the
  confirmation gate, and nothing yet shows that the counts are worth
  carrying. Reconsider once local evidence has shown its value.
- **One store shared by every machine.** Rejected. It would be new
  infrastructure, and the evidence has not yet shown a need for it. Pull
  requests already carry evidence from every machine.

## Consequences

- Abandoned work, and work outside pull requests, can now leave evidence,
  as counts only. The gate stays where it is.
- A new kind of event means a new hook, or a change to an existing one.
  When that change touches a guard, it changes a security control and goes
  in a pull request of its own.
- Accepted: events are per machine. A retrospective sees only the events
  of the machine it runs on. Cloud routines see none, as with the
  rule-usage log today.
- Accepted: an event shows that something happened, but not why, and not
  whether it was right. A guard denial counts the same whether it stopped a
  mistake or was a false positive. Telling the two apart is up to the
  operator when a draft is filed.
- Accepted: the loop never sees the content that would explain an event,
  so some friction will stay unexplained. That is the price of keeping
  session content out.
