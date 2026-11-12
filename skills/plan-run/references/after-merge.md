# After the merge: scratch files and the hand-over

Part of plan-run/SKILL.md sections 6 and 7.

## Scratch files (section 6)

A run keeps them only in its session scratch directory: the one the session-scratch hook named at
session start, or the harness's scratchpad (trimtab-core ADR 0049), or, where neither exists, one
directory under the system temp directory named after its session. The hook removes the first two
when the session ends; tidying now keeps the next item's session clear of them. Remove only this
session's own files there, nothing wider (caches, another session's scratch). A session that did not
do the merged work leaves the other session's directory alone: cloud containers are discarded, and a
directory the merged record names as left behind is the operator's to remove, so report it.

## Handing over (section 7)

This applies when this session did the merged work. Hand over instead, so the next work starts
clean, and stop after the report. The brief is the merged record's `## Next`, plus "run the
authorisation check first". End in the first way that applies:

1. **Started by a parent session:** stop. The parent clears this session and sends it the brief.
2. **Desktop app:** tell the user the next turn starts from that `## Next`, then clear this session
   (`clear_session` with `"self"`), if the tool is available.
3. **Otherwise (plain CLI):** give a paste-ready line — you cannot run `/compact` yourself — of the
   form
   `/compact Focus on <plan> item NN part Y. Run the authorisation check first; the next work is the latest merged record's ## Next.`
   plus the plan path, and nothing about the finished PR beyond its number and merge SHA.

In the cloud there is nothing to hand over: the next merge, labelled `plan:continue`, starts the
next session.
