# After the merge: scratch files and the hand-over

Paths such as `plan-run/SKILL.md` are relative to the skills directory, two levels above this file.

Part of plan-run/SKILL.md sections 6 and 7.

## Scratch files (section 6)

These are the temporary files this session made for the merged work. A run keeps them only in its
session scratch directory: the one the session-scratch hook named at session start, or the harness's
scratchpad (trimtab-core ADR 0049), or, where neither exists, one directory under the system temp
directory named after its session. The hook removes the first two when the session ends; tidying now
keeps the next item's session clear of them. Remove only this session's own files there, nothing
wider (caches, another session's scratch). A session that did not do the merged work leaves the
other session's directory alone: cloud containers are discarded, and a directory the merged record
names as left behind is the operator's to remove, so report it.

## Handing over (section 7)

This applies when this session did the merged work. Hand over instead, so the next work starts
clean, and stop after the report. The brief is the merged record's `## Next`, plus "run the
authorisation check first". End in the first row that applies:

| How this session runs | End it this way |
|---|---|
| Started by a parent session | Stop. The parent clears this session and sends it the brief. |
| Desktop app, with the `clear_session` tool available | Tell the user the next turn starts from that `## Next`, then clear this session (`clear_session` with `"self"`). |
| Desktop app without `clear_session`, or plain CLI | Give a paste-ready line — you cannot run `/compact` yourself — of the form `/compact Focus on <plan> item NN part Y. Run the authorisation check first; the next work is the latest merged record's ## Next.` plus the plan path, and nothing about the finished PR beyond its number and merge SHA. |

A hand-over moves only what comes next, at a part boundary: the merged part's state stays in its
record and PR. Within a part, work does not move between sessions before ready, except to a later
session on the same machine and config dir, which holds the part's round ledger (trimtab-core ADR
0063 §1b); plan-item/SKILL.md says how to size a part so that it need not.

In the cloud there is nothing to hand over: the next merge, labelled `plan:continue`, starts the
next session.
