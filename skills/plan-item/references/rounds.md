# In-session review rounds

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Step 5 of plan-item/SKILL.md (the rounds before the draft), and the one round after each CI fix in
plan-pr/SKILL.md (trimtab-core ADRs 0060, 0062, 0070; the counts are the doctrine record's, the
instance's decision record that trimtab-core ADR 0063 designs against).

## A round

A round runs `/trimtab-review`, `/security-review` and `/code-review` on the branch's current head,
in parallel, folded into one report. A pass that fails or returns nothing usable is named in it, and
the round is incomplete until it runs. Run a failed pass again once on the same head; a second
failure there is a stop. Grade each finding at the higher of the pass's own severity and the
doctrine's finding-severity rule: raise a pass's grade, never lower it. Whether a finding is within
the item's scope is decided only as plan-item/references/scope.md says.

Each finding outside the item's scope becomes its own register entry, unless an entry already
records it; one graded CRITICAL or HIGH is also a stop, in any round. Each round's outcome goes into
the progress record (plan-item/references/records.md). The guard records each round in the part's
ledger (E11); the record is the run's account, the ledger the count.

## Before the draft: one to five rounds

| After round | In-scope findings | Then |
|---|---|---|
| 1 to 4 | None | The review ends. Open the draft (plan-item/SKILL.md step 6). |
| 1 to 4 | Any, whatever their severity | Fix every one (within the fix limits), push, and start the next round. The authorised plan covers these fixes: `/trimtab-review`'s "never auto-remediate" does not apply to them. An in-scope finding the fix limits forbid fixing is a stop, never a register entry. |
| 5 | A CRITICAL or HIGH | Not fixed in session: a stop. |
| 5 | Only MEDIUM or LOW | Not fixed in session: each becomes its own register entry. Then open the draft. |

Five is a ceiling, not a target: a round with no finding within scope ends the review at once.

## After each CI fix: one round

Once the draft is open, each CI fix round (plan-pr/SKILL.md) gets exactly one round on its new head
before a re-review is requested or the PR is marked ready:

| The round finds | Then |
|---|---|
| Nothing within scope | Push the round's outcome line (and any register entries it wrote) as the one push after its head, then request the re-review. |
| In-scope findings it can fix | Fix them, with the outcome line and any register entries, in that one push, then request the re-review. Those fixes get no further round: the next CI review alone covers them. |
| An in-scope finding it cannot fix (any severity) | A stop. |
| Out-of-scope findings | Register entries, in the same one push; a CRITICAL or HIGH among them is a stop. |
