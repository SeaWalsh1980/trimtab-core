# Stops

Every stop in a plan run follows this, whichever skill it happens in (plan-run, plan-item, plan-pr).
Section numbers below are plan-run/SKILL.md's; step numbers are plan-item/SKILL.md's.

## Every stop

**Every stop** reports the blocker and proposes one to three ways forward for the operator (for
example: approve a new plan covering it, make the guarded change by hand, or correct the record in a
reviewed PR).

## A stop before any item PR exists

**A stop before any item PR exists** — the authorisation check (section 2) or this gate failing at
the start of a session — labels the merged PR that started the session `plan:blocked` (through
`gh api` if `gh pr` is blocked), so every stop leaves a label the admin can remove to allow a
restart. A valid restart session has no such PR and labels nothing.

## A stop in an item PR

Set `## Status` to say whether this part finished (`stopped unfinished` if not) and `## Next` to
`blocked: <reason>`; label the PR `plan:blocked` and remove any `plan:continue` added in step 3 (the
labels always follow the final `## Next`); and tell the user, with ways forward. A PR whose own work
is finished can still carry `plan:blocked`; it merges normally, and its merge starts no session —
say so rather than promising one.
