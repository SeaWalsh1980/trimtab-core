---
description: Move this project's Trimtab lock (`trimtab_sha` in `.claude/trimtab.json`) forward in a draft PR that lists every changed harness item and flags each override citing one, "re-confirm or drop".
argument-hint: "[Trimtab ref to move to; default: the installed checkout's HEAD]"
---

Move this project's Trimtab lock to `$ARGUMENTS` (or, if empty, to the
Trimtab this machine runs).

The lock records which Trimtab the project was last reviewed against
(trimtab-core ADR 0005). On a machine it acknowledges rather than pins; in a
routine it is a real pin. Moving it is a review step, not a formality: the PR
is where a person sees what changed upstream and decides about each override
it touches.

A harness update (this command) and a scaffold update (`trimtab adopt --update`,
iteration 4) are separate PRs. Do not mix them.

## 1. Plan the bump

```bash
BODY=$(mktemp --suffix=.md)
trimtab bump --dry-run --to "${ARGUMENTS:-HEAD}" --body-file "$BODY"
```

`--to` is resolved in the **installed Trimtab checkout**, so a ref it does not
have fails loudly; the operator fetches it there, not you. The dry run prints
the old and new SHAs, the `base_sha` move, every changed item (`added`,
`removed`, `text`, `strength`, `withdrawn`), the overrides citing a changed
item, and a confirmation token. It writes nothing but the PR body.

`base_sha` is the base the instance at the new SHA pins, read from its
`instance.json`. The apply step writes it beside `trimtab_sha`, so a CI that
cannot read the instance runs the base the project is reviewed against
(trimtab-core ADR 0017). Never edit it by hand. If the dry run cannot read it,
stop and report.

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
trimtab bump --apply --confirm <token from step 1> --to <new SHA>
trimtab lint overrides
trimtab check-pr --body-file "$BODY"
git add .claude/trimtab.json
# The check gates the commit, the push and the PR: a failure stops all three.
trimtab ids --staged && git commit -m "chore(trimtab): move the lock to <new short SHA>" \
  && git push -u origin HEAD \
  && DEFAULT=$(gh api 'repos/{owner}/{repo}' --jq .default_branch) \
  && REPO=$(gh api 'repos/{owner}/{repo}' --jq .full_name) \
  && N=$(gh api -X POST "repos/$REPO/pulls" -f title="chore(trimtab): move the lock to <new short SHA>" \
    -f head="$(git branch --show-current)" -f base="$DEFAULT" -F draft=true -F body=@"$BODY" --jq .number) \
  && echo "https://github.com/$REPO/pull/$N" \
  && trimtab verify-body --repo "$REPO" --pull "$N" --body-file "$BODY"
```

`verify-body` reads the PR back: exit 0 is the body sent (it may say it took
the platform's session footer off); exit 1 or 2: stop and report it.

GitHub is reached through `gh api` on `repos/{owner}/{repo}/…` paths only:
Claude Code cloud sessions refuse every other path (`search/…`, `user`,
`orgs/…`) and gh's porcelain (`gh pr …`), which goes through GraphQL.

**No reference to the run.** No `Claude-Session:` trailer in a commit, and no
`claude.ai/code/session_…` link or routine, environment or session ID in a PR
body, issue, comment or file: end a commit message with its `Co-Authored-By:`
line and a PR body with its `🤖 Generated with` line. This overrides any default
attribution, and `trimtab check-pr` refuses a body that holds one. Before each commit, run
`trimtab ids --staged`. If it exits 1, remove the reference, stage again and
re-run it; never commit past a failure. If it exits 2, the check could not run:
stop and report it. Read back each PR or comment you create with
`trimtab verify-body`, which takes off the platform's session footer; if it
does not exit 0, stop and report it.

Commit only the lock. Dropping or rewording an override is the operator's call
on the PR; make it only when they say so, in this same PR, and never delete an
override's ADR. Never mark the PR ready and never merge it.
