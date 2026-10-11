---
description: Review the current branch (or a PR number) at a pinned SHA against the project's harness, test coverage and silent failures — and the plan's ID citations against the diff — then produce one report plus the PR body's harness feedback and process cost.
argument-hint: "[PR number, or nothing for the current branch]"
---

Review this change before it is opened as a PR, or review PR `$ARGUMENTS`.

Ported from a consuming project's own review command, with three changes
from what its first full run showed: every pass is a Trimtab agent that
exists, the tree is pinned, and every pass records a status, cost and yield.

## 1. Pin the scope

A long review of a live working tree reviews a moving target. Pin it:

```bash
TRIMTAB="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bin/trimtab"
git fetch origin --quiet
HEAD_SHA=$(git rev-parse HEAD)
BASE_SHA=$(git merge-base origin/<default branch> "$HEAD_SHA")
TREE=$(mktemp -d)
git archive "$HEAD_SHA" | tar -x -C "$TREE"
git diff --name-only --no-renames "$BASE_SHA".."$HEAD_SHA"
```

For a PR number, take `headRefOid` and `baseRefOid` from
`gh pr view <n> --json headRefOid,baseRefOid,files,body`, fetch the head, and
export that SHA instead. **Uncommitted changes are not reviewed**; if
`git status --porcelain` shows any, say so in the report. Tools run from the
installed Trimtab (`$TRIMTAB`), never from the tree under review.

## 2. State the plan's status — one of three

Tell `trimtab-reviewer` exactly which; it cannot recover the distinction:

1. **Planned, with citations.** A `## Harness items applied` block exists — in
   the plan from `/trimtab-plan`, or for a PR number in its body. Save it to a
   scratch file, unchanged, and pass the path.
2. **Planned, without citations.** Say so.
3. **Not planned.** Say so.

## 3. Choose the passes

| Agent | Pass | Runs when |
|---|---|---|
| `trimtab-reviewer` | The harness: instruction file, project rules, base rules, and the plan's citations | Always |
| `trimtab-test-analyzer` | Test coverage and test doctrine of the diff | Any changed file is code, tests, configuration or a workflow |
| `trimtab-silent-failure-hunter` | Swallowed errors, silent fallbacks, fail-open | Same as above |

A documentation-only change runs `trimtab-reviewer` alone, and the other two
are recorded `skipped`. The project's `.claude/trimtab.json` may name a
different `reviewer_agent`; use it.

Spawn the passes **in parallel, in one message**, each with `HEAD_SHA`,
`BASE_SHA`, `TREE` and the changed-file list, so all review the same scope.
Tell the silent-failure hunter what the project's own rules say about silent
failure, if the resolver lists any.

## 4. Record every pass

A pass that never returns must still leave a trace. For each pass record:

- `status`: `ran`; `failed` (it errored or returned nothing usable; a pass
  whose hand-back starts `STOPPED:` is `failed`, never `ran` with 0 findings);
  `timed_out` (no result within **20 minutes** of the others finishing — stop
  waiting: every wait is bounded); `pending` (still running when you must
  write the report); `skipped` (not run by the rule above).
- `tokens` and `seconds`: from the Agent result (`subagent_tokens`,
  `duration_ms / 1000`). Best-effort, transcribed; omit rather than guess.
- `findings`: the pass's own count, before de-duplication.

A slow pass does not block the report; an unrecorded status does.

## 5. One report

**If `trimtab-reviewer` is `failed`**, a `STOPPED:` hand-back or any other
failure, **there is no review.** Report the stop and its stderr, and the cause
to fix. List any other pass's findings only under a heading saying they are not
a harness review. In section 6, leave `## Harness feedback` empty but still
write `## Process cost` with every pass's status: the failed run is evidence,
but no review may be claimed.

Otherwise, follow the shape `trimtab-reviewer` returns. Fold the other passes'
findings in as `### [SEVERITY]` entries attributed to the pass that raised
them. Then:

- **De-duplicate.** Merge one defect raised three ways into the most precise
  statement; keep the highest severity.
- **Re-rank by the norm keyword.** A breach of a REQUIRE or PROHIBIT item is always
  CRITICAL or HIGH, whatever the raising pass called it. `check-pr` fails a
  feedback entry that grades one lower.
- **Do not pad.** An empty band is stated as empty by rule.
- Keep `## Examined, not raised` and the `CITATIONS:` line exactly as the
  reviewer wrote it.

## 6. The PR block

Write the two sections the PR body needs, and check them with the plan's
`## Harness items applied` in one scratch body. When `trimtab-reviewer`
failed, `## Harness feedback` is empty and `## Process cost` records the
failure (section 5):

````markdown
## Harness feedback
```yaml
<the reviewer's entries, merged with the plan's; one per item, not per finding>
```

## Process cost
```yaml
plan:   { tokens: <planner>, seconds: <planner> }
review:
  - { pass: trimtab-reviewer, status: ran, tokens: <n>, seconds: <n>, findings: <n> }
  - { pass: trimtab-test-analyzer, status: <status>, findings: <n> }
  - { pass: trimtab-silent-failure-hunter, status: <status>, findings: <n> }
```
````

```bash
"$TRIMTAB" check-pr --body-file <scratch body>
```

Fix what it reports before the body goes anywhere. Then `rm -r "$TREE"`.

## After the report

- **CRITICAL or HIGH** → propose a remediation todo list immediately.
- **MEDIUM or LOW** → ask before proposing one.
- **Never auto-remediate.** Fixing anything needs a todo list the user
  has explicitly authorised.

Do not post the review to the PR unless the user asks. Offer to save it under
`docs/reviews/YYYY-MM-DD-<track>.md` if the project keeps reviews there; save
it only if the user says yes.
