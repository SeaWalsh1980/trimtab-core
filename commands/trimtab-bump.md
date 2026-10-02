---
description: Move this project's Trimtab lock (`trimtab_sha` in `.claude/trimtab.json`) forward in a draft PR that lists every changed harness item and flags each override citing one, "re-confirm or drop".
argument-hint: "[Trimtab ref to move to; default: the installed checkout's HEAD]"
---

Move this project's Trimtab lock to `$ARGUMENTS` (or, if empty, to the
Trimtab this machine runs).

The lock records which Trimtab the project was last reviewed against (plan 0009,
section 3). On a machine it acknowledges rather than pins; in a routine it is a
real pin. Moving it is a review step, not a formality: the PR is where a person
sees what changed upstream and decides about each override it touches.

A harness update (this command) and a scaffold update (`trimtab adopt --update`,
iteration 4) are separate PRs. Do not mix them.

## 1. Plan the bump

```bash
TRIMTAB="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bin/trimtab"
BODY=$(mktemp --suffix=.md)
"$TRIMTAB" bump --dry-run --to "${ARGUMENTS:-HEAD}" --body-file "$BODY"
```

`--to` is resolved in the **installed Trimtab checkout**, so a ref it does not
have fails loudly; the operator fetches it there, not you. The dry run prints
the old and new SHAs, every changed item (`added`, `removed`, `text`,
`strength`, `withdrawn`), the overrides citing a changed item, and a
confirmation token. It writes nothing but the PR body.

Stop and report if it says the lock is already current, or if an item was
`removed` (a vanished ID without a withdrawal stub is a Trimtab defect: raise it
upstream rather than bumping past it).

## 2. Read what changed

For each changed item the project cites or overrides, read its new text in
`HARNESS.md` and the rule file at the new SHA. For each **flagged override**,
compare the new base text with the override's `text` and `reason`, and write
one line into the PR body under its checklist entry: still needed and why,
needs rewording, or can be dropped because upstream now says the same. Do not
tick the box: the operator's tick is the re-confirmation, because once the lock
moves `trimtab lint overrides` compares against the new SHA and passes.

If a changed item is now REQUIRE or PROHIBIT and an override cites it, the
override is no longer allowed (only a PREFER or AVOID may be overridden): say so at the top of the body; it must be
dropped and, if the project cannot comply, raised upstream.

## 3. Open the draft PR

On a new branch from the default branch (`trimtab/bump-<new short SHA>`):

```bash
"$TRIMTAB" bump --apply --confirm <token from step 1> --to <new SHA>
"$TRIMTAB" lint overrides
"$TRIMTAB" check-pr --body-file "$BODY"
git add .claude/trimtab.json
git commit -m "chore(trimtab): move the lock to <new short SHA>"
git push -u origin HEAD
gh pr create --draft --title "chore(trimtab): move the lock to <new short SHA>" --body-file "$BODY"
```

Commit only the lock. Dropping or rewording an override is the operator's call
on the PR; make it only when they say so, in this same PR, and never delete an
override's ADR. Never mark the PR ready and never merge it.
