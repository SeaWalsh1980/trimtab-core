# 0015. A consumer's CI checks the block without the doctrine

## Status

Accepted.

## Context

`trimtab check-pr` resolves every cited ID against a registry built from the
instance's `rules/` plus the project's own rule files (ADR 0005, ADR 0008). On
an operator's machine the instance is installed, so the full check runs
locally, in the `pr-body-check` hook. In CI it is the real gate: a PR opened
from a browser never passes through the hook.

An instance's CI has its own doctrine in the checkout. A consuming project's CI
does not. The instance is usually a private repository, and the consumer's
runner has no access to it unless a credential for it is stored in the
consumer. Before this change, `check-pr` had no mode that worked without an
instance, so a consumer could either store such a credential or run no CI check
of the block at all.

The constraints:

- The block format, its marker and the default `check-pr` behaviour are a
  published contract (ADR 0007) and must not change.
- A consumer's own IDs live in its checkout, so they can still be resolved
  in full.
- The body is untrusted. No message may quote it, whichever mode runs.
- One parser serves every caller (ADR 0005). A second validator for CI would
  drift from it.

## Decision

- **`check-pr --schema-only`** checks everything about the block that needs no
  instance: the marker, the three headings, one yaml fence per section, the
  required fields, the feedback kinds and scopes, the review-pass statuses,
  and `id: none` needing a why.
- **IDs under the project's prefix are resolved** against the project's own
  rule files, as in a full check: an unknown or withdrawn project ID fails, and
  a project REQUIRE or PROHIBIT graded MEDIUM or LOW fails.
- **Every other ID is checked for shape only** (`ITEM_ID`) and never looked
  up. So an unknown instance ID passes, and the strength check (a binding
  item graded below HIGH) is skipped for it. A project with no lock has no
  prefix, so every ID is shape-only.
- **The flag reads no instance.** `--instance` with it is a usage error (exit
  2) naming both flags, and `TRIMTAB_INSTANCE` in the environment is ignored.
  A project lock or rule file that cannot be read fails the check; it never
  falls back to shape-only.
- **The same parser runs.** `check_body` takes `schema_only` and
  `project_prefix` as keyword arguments. Without them, behaviour is unchanged.
  `registry.lookup.project_items` reads the project's items and prefix alone,
  and `registry_for` now builds on it.

## Options considered

- **A full check in CI, with a read-only credential for the instance stored in
  the consumer.** Rejected. It puts a long-lived credential to a private control
  plane into a second repository, where it is a security control of its own,
  with expiry and rotation tied to one account. Dependency-update PRs get no
  repository secrets, so they would fail the check anyway.
- **No CI check of the block in a consumer.** Rejected. The local hook only sees
  PRs opened from a session, so a PR opened in the browser would go unchecked,
  and CI is meant to be the gate.
- **A separate, lighter validator for CI.** Rejected. Two parsers of one format
  disagree sooner or later, and the block is read long after it is written.
- **Treat every ID as shape-only, the project's included.** Rejected. The
  project's rules are in the checkout, so skipping them would give up a check
  that costs nothing.

## Consequences

- A consumer's CI can gate the block with no secret and no access to the
  instance.
- Accepted: an unknown instance ID, or an instance item graded below its
  strength, passes a consumer's CI. The local `pr-body-check` hook, which runs
  the full check against the installed instance, catches it before the PR
  opens. The retro, which reads merged bodies against the full registry, counts
  such a block invalid and names it.
- Accepted: under `--schema-only` a withdrawn instance ID is not flagged in CI,
  for the same reason and with the same backstops.
- Default `check-pr` behaviour, the block's format and its marker do not change.
