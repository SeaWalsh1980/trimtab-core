---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Give me a command and prompt to run in a claude-code session. This is an interactive session, not a plan run. In ~/work/shop-sync, the Zoho Inventory sync mustn't double-create items when a SKU's name contains an apostrophe; add a failing test first, fix it, and open a draft PR. The Write tool is refused in this session, so no prompt file can be created with it.
