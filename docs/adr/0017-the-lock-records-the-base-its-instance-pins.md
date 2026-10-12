# 0017. The lock records the base its instance pins

## Status

Accepted.

## Context

A project's lock (`.claude/trimtab.json`, ADR 0005) pins the *instance* SHA in
`trimtab_sha`. The instance's `instance.json` pins the base (ADR 0008), so one
SHA fixes both the doctrine and the mechanism the project was reviewed
against. Recording the base SHA in the lock as well was rejected when the lock
was designed: it would be a second pin that could disagree with the
instance's own.

ADR 0015 then gave a consumer's CI a way to run base code, `check-pr
--schema-only`, with no access to the instance. That CI has to choose a base
SHA to fetch, and it cannot read `instance.json` at `trimtab_sha`, because the
instance is usually private and storing a credential for it was rejected in
ADR 0015. So the consumer writes the base SHA somewhere itself, usually as a
literal in its workflow. That literal is the second pin the lock design
avoided, with no check on it at all. Nothing moves it when the lock moves, and
a dependency bot does not track it. After a base change to `check-pr`, the
consumer's CI keeps checking with the old base: too lenient, or red once the
project's blocks depend on the new schema.

The constraints:

- The instance's own pin stays the authority. Whatever the lock records about
  the base is derived from it, never chosen.
- A consumer's lock is untrusted input, and a consumer's CI runs what it
  names. A value that picks code to run must be a full SHA (a short one can
  become ambiguous, and a ref can move).
- The change must be additive: an older base reading a newer lock must still
  work.

## Decision

- **The lock gains an optional `base_sha`**: the base SHA that `instance.json`
  pins at the lock's `trimtab_sha`. The lock reader refuses one that is not a
  full 40-character lower-case SHA. The schema version stays 2: older readers
  ignore keys they do not know.
- **`trimtab bump` derives and writes it.** `plan` reads `instance.json` at the
  new instance SHA (`instance.at_commit`) and fails loudly if it cannot. `apply`
  writes `base_sha` beside `trimtab_sha` behind the same confirmation token,
  and refuses if either value changed since the dry run. A lock whose
  `trimtab_sha` is current but whose `base_sha` is missing or stale still has
  something to bump. The PR body names the base move, with a compare link when
  there was an old value.
- **The drift hook checks it.** At session start, when the lock has a
  `base_sha`, the hook warns if it is not the base pinned at `trimtab_sha`, or
  if that cannot be read. A lock without the field is not warned about.
- **How a consumer's CI should read it** (guidance, not code in the base): take
  `base_sha` from the lock and refuse anything but 40 hex characters. Fetch the
  base's default branch and prove the SHA is an ancestor of it (`git merge-base
  --is-ancestor`) before running any of its code. Check out the SHA and verify
  `HEAD` equals it. The ancestry check matters because the lock is not a
  security control: without it, any edit to an ordinary file could make CI
  fetch and run an arbitrary commit, a fork's included.

## Options considered

- **Keep the base SHA as a literal in the consumer's workflow.** Rejected. It
  is the second pin with no check. Moving it is a change to a security control
  every time, and it is moved by memory or not at all.
- **Have the instance's CI compare each consumer's literal with its own pin.**
  Rejected. It needs read access to every consumer, which for a private
  consumer is a stored credential, and the instance's head is the wrong
  reference anyway: a consumer is reviewed against the instance at its lock,
  not at the instance's latest commit.
- **Have `bump` also patch the consumer's workflow.** Rejected. Every lock move
  would become a security-control change, and the base would have to know each
  consumer's workflow layout.
- **A documented manual step, with a check on the operator's machine.**
  Rejected. A rule that has to hold should be enforced by a tool, not by a
  step someone has to remember. And the literal would still have to move on
  every base change.
- **A floor SHA in the consumer's workflow, which `base_sha` must descend
  from.** Not adopted. A base too old to have `--schema-only` already fails.
  A lock edit is visible in review, and the drift hook flags a `base_sha` that
  disagrees with the instance. A consumer may still add one.

## Consequences

- A consumer's CI follows its lock. It runs the base the project was reviewed
  against, and moves when `/trimtab-bump` moves the lock, with no change to a
  security control.
- Accepted: the lock now holds a second copy of the base pin, the thing its
  original design avoided. The copy is derived, written by one tool, and
  checked against the authority at every session start. The literal it
  replaces had none of those properties.
- Accepted: the consumer's CI cannot itself prove that `base_sha` matches the
  instance (it cannot read the instance). The ancestry check limits it to code
  merged on the base's default branch, and the drift hook and review catch a
  mismatch.
- Accepted: until a project's lock is bumped by a base with this change, its
  lock has no `base_sha`. A consumer CI that reads the field fails closed
  until then.
