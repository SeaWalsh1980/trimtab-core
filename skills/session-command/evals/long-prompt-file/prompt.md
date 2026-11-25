---
tags: [skill-creator]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Give me a command and prompt to run in a claude-code session. In ~/work/shop-sync: the Zoho Inventory sync mustn't double-create items when a SKU's name contains an apostrophe (e.g. Dog's Bowl) or `$` characters; add a failing test first, fix it, keep the existing idempotency key behaviour, run the architecture tests, and open a draft PR. Mention what done looks like.
