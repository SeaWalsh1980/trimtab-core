# 0007. The PR block is versioned by a marker

## Status

Accepted.

## Context

The three sections of the PR block (ADR 0005), `## Harness items applied`,
`## Harness feedback` and `## Process cost`, form a contract. On one side is
whoever writes a PR body: a session, the planner agent or a person. On the
other is everything that reads it: `trimtab check-pr`, the `pr-body-check.sh`
hook, CI's PR-body job and ingest. A contract like this must carry an
explicit version, and its schema must never change silently.

A body outlives the code that parsed it when it was written:

- Merged PR bodies are the loop's durable evidence, and they are read again
  later. ADR 0006 rebuilds the ledger from them over an 8-week window.
- Cloud routines run the version pinned in each project's lock, so an older
  parser can meet a body written for a newer one.
- Different machines can run different installed versions.

So the reader that matters is the **old** one. A version marker helps only if
the readers deployed before any version 2 exists already refuse versions they
do not know. That is why it went in while there was only version 1.

## Decision

- **The marker** is an HTML comment, `<!-- trimtab-block: N -->`, on a line of
  its own before the first section. GitHub does not show it when it renders
  the PR. It counts only when it is the whole line and outside code fences,
  so prose, inline code or a fenced example that mentions a marker does not
  set the version. The first draft of the body for this very change showed
  that rule was needed.
- **No marker means version 1.** Existing bodies stay valid with no edit. The
  PR template for consuming projects (`templates/pull_request_template.md`)
  includes `<!-- trimtab-block: 1 -->`, so new bodies state their version.
- **Readers refuse versions they do not know.** The parser accepts the
  versions listed in `BLOCK_VERSIONS`, which today is just `1`. Any other
  positive integer fails as `unknown-version`, with a message that the body
  is newer than this Trimtab can read. A malformed value, or two markers that
  disagree, fails as `bad-version`. Messages show only a parsed integer,
  never the marker's raw text, because a PR body is untrusted input.
- **Ingest leaves a newer body unlabelled.** It neither records the body nor
  labels it `trimtab-ingested`, and every run lists it, so a newer Trimtab
  will still find it and count it. It is not recorded as an invalid block,
  because that would lose the evidence.
- **Bump the version only for a breaking change**: a field removed, renamed
  or given a new meaning, or a new required section. Additive changes, such
  as a new optional field or a new value that old readers can safely reject,
  stay at version 1. The change that bumps the version also adds the new
  number to `BLOCK_VERSIONS` and teaches the parser to read it.

## Options considered

- **No versioning: change the format and update every reader.** Rejected.
  Readers are pinned and bodies are read again later, so an old reader would
  read a changed block as the old format without saying so. That is a silent
  schema change.
- **A mandatory `version:` field in every block, starting now.** Rejected.
  Every PR body would need a field that almost never changes, and a missing
  field would become a new way to fail the check, with no benefit until a
  version 2 exists.
- **A `version:` key inside the `## Harness items applied` yaml.** Rejected.
  It ties the version to one section's shape. A version 2 that restructured
  that section could not be detected before parsing it.
- **The version in a heading, such as `## Harness items applied (v2)`.**
  Rejected. A suffix is easy to drop or mistype when a person edits the
  body, and it clutters every PR.
- **Wait until a version 2 is needed, then add the marker.** Rejected. Readers
  already deployed would not know about the marker and would read a version 2
  body as version 1. The refusal has to ship before the first bump.

## Consequences

- Nothing existing changes. Unmarked bodies and bodies marked `1` read as
  before.
- A project whose installed or pinned version is older than a body's version
  fails that body in `check-pr`, in the hook and in CI until it is updated.
  That loud failure is the point.
- Until then, those PRs stay unlabelled and are read again on every run,
  taking up places in each run's `--limit`. Every run names them, so the
  backlog stays visible.
- Accepted: anyone who can edit a PR body can change or remove the marker.
  The body is already self-reported evidence, and CI re-checks the block on
  every edit.
