"""The plan-run enforcement gate (docs/adr/0063-the-plan-run-enforcement-gate.md).

`record` parses the doctrine record's §Enforcement table, `manifest` reads this base's map of
rows to hooks and tests, and `gate` gathers the facts and decides each row. Only `gate` touches
git, the network or the file system.
"""
