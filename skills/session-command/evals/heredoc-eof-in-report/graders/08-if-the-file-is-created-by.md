---
type: llm
focus: trace
weight: 1
---

If the file is created by a heredoc command, the command removes any existing file of that name first (rm -f) and the launch is chained after the whole creation, so a stale file cannot be launched
