# 0005. Harness items by ID, and the evidence loop

## Status

Accepted.

## Context

A harness is the set of instruction rules, guards, settings and agents that
shape a Claude Code session. Improving one tends to be anecdotal: someone
remembers that a rule caused friction, or forgets that it did. Looking at
real review runs showed what that costs:

- Review passes named agents that did not exist and never ran, and nothing
  noticed.
- In one run, 34 correct rule citations were counted as "invented", because
  they were matched against rule files by file name and heading text, and
  both had moved.
- Passes that never returned left no trace.
- The one metric that mattered lived in a report that was thrown away.

A loop that improves the harness needs evidence it can count. That needs a
stable name for each thing being judged, and a durable place where judgements
are recorded.

## Decision

**The harness is made of items, and every item has an ID.** The first item
type is the instruction rule. An ID has the form `PREFIX-<local id>`. The
prefix comes from the rule file's frontmatter (`id_prefix`), and the local id
is the section number, as in `CORE-6.2`. A project's own rules use
`<project prefix>-<SLUG>-<section>`, as in `APP-LAYERING-3`. The slug is in
frontmatter, not taken from the file name, so renaming a file changes no ID.
An item is a `##` or `###` heading that starts with a number. IDs are assigned
once and are never reused or renumbered. A withdrawn item keeps its heading,
marked withdrawn, with a forward link. An item's **strength** is the
strongest norm keyword it contains: REQUIRE, PROHIBIT, PREFER, ALLOW or
AVOID. Everything cites the ID, never the file and heading.

`bin/trimtab registry` generates `HARNESS.md` from the instance's rules.
`--check` fails if the file is stale, if an ID is duplicated, or if an ID has
vanished without a withdrawal stub.

**Projects layer on the base; they do not fork it.** Each consuming project
commits a lock, `.claude/trimtab.json`, which records its ID prefix and the
SHA it has adopted (ADR 0008). On a machine the drift hook only warns when the
installed version differs. In the cloud, a routine clones at that SHA, so
there it is a real pin. A project never copies
or edits a base item. It deviates only through entries in
`.claude/rules/overrides.md`. Each entry names a base ID, gives the
replacement text, the reason and a decision record. Only PREFER and AVOID
items can be overridden. `trimtab lint overrides` checks this, and fails when
the base text of an overridden ID has changed since the project's pinned SHA.

**Every pull request body carries a block in three sections**, each written
as `yaml`:

- `## Harness items applied`: required, with at least one entry. An `id:
  none` entry must say why.
- `## Harness feedback`: present, but may be empty. Each entry has a `kind`
  (missed, conflict, ambiguous, obsolete or gap), a `scope` (project or
  upstream) and the `evidence`.
- `## Process cost`: present, but may be empty. Each review pass records a
  status (ran, timed_out, failed, skipped or pending), so a pass that never
  returned still leaves a trace.

One parser serves `trimtab check-pr`, a local hook and CI, and it never
quotes the body in its messages. The local hook is policy, not a guard, so it
fails open.

**Feedback is tallied per item ID.** Merged pull requests are ingested by
watermark: each one is recorded, then labelled `trimtab-ingested`, keyed by
PR number, so a run that dies between the two steps still counts it once.
Feedback is tallied per (ID, scope) by distinct pull request. Citation counts
are reported but never scored, because a count rewards citing for its own
sake.

**A threshold opens a proposal.** When feedback on the same ID and scope
comes from **2 or more** pull requests, the loop drafts one proposal for
that ID. Upstream feedback becomes an issue on the instance labelled
`harness-feedback`. Project feedback becomes a draft pull request in the
project. Across projects, the instance reconsiders a base default when 2 or
more consuming projects override the same ID. Recording, labelling and
opening issues are side effects, so each sits behind a gate. A dry run prints
the plan and a confirmation token, and `--apply --confirm <token>` redoes the
plan and aborts if it no longer matches.

## Options considered

- **Cite by file and heading.** Rejected. Both move, matching them as text
  produced 34 false "invented" citations, and it cannot be checked
  mechanically.
- **Vendor base rules into each project.** Rejected. Copies drift apart
  silently and produce textual merge conflicts.
- **A git subtree or submodule of the rules.** Rejected. It has the same
  copying problem with more tooling, and a pinned submodule blocks
  improvements made on the machine.
- **A true per-project pin on each machine.** Rejected. Every project on a
  machine reads one installed harness, so a pin per project would need a
  checkout and install per project. The lock acknowledges the version on a
  machine and pins it in the cloud.
- **Automatically adopt one project's change into another.** Rejected. One
  project's deviation is not evidence about another. Upstream changes need 2
  or more consumers and a person to decide.
- **Add the PR-body check to a guard.** Rejected. Guards are security
  controls and fail closed (ADR 0001). A format check is policy and must fail
  open, or a formatting bug would block every Bash call.
- **Mine session transcripts for feedback.** Rejected. Transcripts hold the
  operator's own words and are not the loop's to read.
- **Derive project slugs from file names.** Rejected. A rename would change
  every ID in the file.
- **Number sections by position.** Rejected. Inserting a section would
  renumber everything after it.
- **Let ingest write without a gate.** Rejected. Labels are a write to
  GitHub, and a side effect needs a preview and a confirmation that can go
  stale.

Several mechanisms are borrowed from existing self-improving agent tools:
acting only on repeats, one proposal per item, two loop levels (project and
upstream), and letting evidence decay after eight weeks (ADR 0006).

## Consequences

- Citations can be checked, in PR bodies and in overrides, against one
  generated registry.
- Adding or withdrawing an ID-bearing heading means regenerating
  `HARNESS.md`. CI fails when it is stale.
- Every PR body in a consuming project must carry the block.
- Accepted: strength is decided per section, so most sections read as
  REQUIRE. An override has to name the PREFER or AVOID clause it replaces.
- Accepted: the evidence is self-reported. A PR body can be edited, and CI
  re-checks the block on every edit.
- The local PR-body hook adds about 3 ms to every Bash call. Measured as
  whole-process wall time: on a Bash call it does not match, p50 was 2.6 ms
  and p95 5.6 ms (n = 400); a full check of a valid `gh pr create` body took
  71.4 ms p50 and 90.3 ms p95 (n = 100). For comparison, `guard-paths` took
  9.5 ms p50 on the same non-matching call.
