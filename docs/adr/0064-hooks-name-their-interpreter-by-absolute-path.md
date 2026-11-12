# 0064. Hooks name their interpreter by absolute path

## Status

Accepted on merge. It is a change to a security control (SEC-7), the guard hooks, authorised in
chat for this change.

## Context

Every hook under `hooks/` started `#!/usr/bin/env bash`, and `rule_usage.py` started
`#!/usr/bin/env python3`. `settings.base.json` runs each hook by its path,
`"$HOME/.claude/hooks/<name>.sh"`, so the kernel reads the shebang and `env` looks for `bash` in
the PATH Claude Code inherited. On Debian that PATH puts the user-writable `~/.local/bin` before
`/usr/bin`. A session that writes `~/.local/bin/bash` therefore swaps out every guard
(guard-paths, guard-bash, guard-github, guard-secrets, guard-security, guard-mcp, session-mode
and the rest) before any line of a hook runs, including any PATH fix inside it. Review round 4
of the plan-run enforcement gate work found this.

## Decision

Each hook names its interpreter by absolute path: `#!/bin/bash` for the shell hooks and
`#!/usr/bin/python3` for `rule_usage.py`. `rule-usage.sh` runs `rule_usage.py` through
`python3` rather than through its shebang, so that line is for consistency. `run_module.py` has
no shebang and is unchanged.

`tests/trimtab/test_hooks.py` (`HookShebangs`) fails if any file under `hooks/` has a shebang that
uses `env` or an interpreter outside `/bin` or `/usr/bin`. `ShebangFault` tests the checker
itself, so the scan cannot pass by matching nothing.

## Options considered

| Option | Outcome |
|---|---|
| Absolute shebang in each hook | **Chosen.** The kernel resolves the interpreter without PATH, and the change stays inside `hooks/` |
| Register hooks as `/bin/bash "<path>"` in `settings.base.json` | Rejected here: it changes the hook registration, which only the operator may change (SEC-7). It would also leave each hook's shebang untested and in charge whenever a hook runs by its path, for example from the test suites |
| Keep `env` and reset PATH inside each hook | Rejected: `env` has already chosen `bash` before the hook's first line runs, so a reset inside the hook comes too late |

## Consequences

- A planted `bash` earlier in PATH no longer replaces the hooks' interpreter.
- **A host with no `/bin/bash` runs without its guards.** The kernel cannot start the hook, the
  shell reports exit 127, and Claude Code treats any exit code other than 2 as "allow" (README,
  Platforms). Every hook fails to start, but nothing blocks: this fails open. The SessionStart probe
  in `settings.base.json` only checks `[ -x hook ]`, so it does not catch this. Debian, the only
  tested platform, always has `/bin/bash`: `/bin` is a link to `/usr/bin` there. A probe for the
  interpreter is a change to `settings.base.json` for the operator (SEC-7) and is not part of
  this record.
- **On macOS the hooks now always run under the system bash 3.2.** Previously a bash 4 or later
  earlier in PATH was enough; now the README's bash 3.2 warning applies to every Mac.
- The hooks still run `python3`, `jq`, `git` and other tools by bare name from the inherited
  PATH, so a planted `~/.local/bin/python3` can still subvert a guard's parsing. This record
  closes only the interpreter. Resolving those tools by absolute path, or resetting PATH at the
  top of each hook, is separate work.
