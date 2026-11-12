# 0065. Hooks reset PATH before their first command

## Status

Accepted on merge. It is a change to a security control (SEC-7), the guard hooks and their
tests, authorised in chat for this change. It builds on ADR 0064.

## Context

ADR 0064 fixed the hooks' interpreter: each shell hook starts `#!/bin/bash`, so a planted
`~/.local/bin/bash` no longer replaces it. It left the tools the hooks run by bare name: `python3`,
`jq`, `git`, `grep`, `sed`, `readlink` and the rest. They still come from the PATH Claude Code
inherited, which on Debian puts the user-writable `~/.local/bin` before `/usr/bin`. A session that
writes `~/.local/bin/python3` subverts guard-github, whose whole decision runs in
`python3 … run_module.py`, as well as guard-mcp, pr-body-check and the payload parsers of
guard-paths, guard-bash, guard-secrets and guard-security. guard-paths does not refuse a write to
`~/.local/bin`.

## Decision

The first statement of every `hooks/*.sh` is `export PATH=/usr/bin:/bin`, after the comment header
and before `set` or any command. The line is exact, so a test can find it.

- `session-path.sh` resets its own PATH like the rest. The session's PATH is a different thing: it
  is the line the hook writes to `CLAUDE_ENV_FILE`, which keeps `"$PATH"` literal for Claude Code
  to expand. The reset never reaches it.
- `session-mode.sh` is also a library that guard-paths and session-scratch source. Sourced, it
  sets the caller's PATH again, to the value the caller has already set.
- Exported, so `python3`, `git` and any child the hook starts inherit the same PATH.

"Before any command" means any command in the hook. Bash itself reads `BASH_ENV` and imports
exported functions (`BASH_FUNC_name%%`, which also take precedence over PATH) from the hook's
environment before line 1. That environment is Claude Code's, not the session's: an inline prefix
on a Bash call never reaches a hook, and `settings.json` `env` is a control (SEC-7). So those stay
outside what this record addresses.

`tests/trimtab/test_hooks.py` (`HookPathReset`) fails if any shell hook's first statement is not
the reset. It also runs each hook with recording stubs in `~/.local/bin`, first on PATH as on
Debian, one stub for every word in the hooks that names an executable in `/usr/bin` or `/bin`.
Each hook gets a payload for its own event that takes it past its fast path, and the test fails,
naming the hook, if any stub ran. A control runs, for every hook, a copy whose reset puts
`~/.local/bin` first again, and requires a stub to run, so no hook's empty record can come from a
payload that stops before any tool.

pr-body-check fails open by design (ADR 0005), and the reset narrows which `python3` it can use.
Each of its fail-open exits now says on stderr that the body went unchecked, and why. Bootstrap
holds the `python3` in `/usr/bin:/bin` to the 3.11 floor and requires PyYAML there, beside its
existing checks of the `python3` first on the operator's PATH.

Cases that hide or replace a tool by narrowing PATH can no longer do it from outside the hook.
They run a copy of `hooks/` whose reset names the stub directory instead: `path_hooks` in
`tests/guard-tests.sh`, `hooks_with_path` in `tests/trimtab/hook_fixture.py`. The copy differs from the original
in that one line. A hook without exactly one canonical reset is not copied, and the case fails.

## Options considered

| Option | Outcome |
|---|---|
| Fixed `export PATH=/usr/bin:/bin` first in each hook | **Chosen.** One line per hook, no state, and a test can check it mechanically. Debian keeps every tool the hooks use in `/usr/bin` |
| Reset PATH inside the hook *instead of* the absolute shebang | Rejected in ADR 0064: `env` has already picked `bash` before the hook's first line. With the shebang absolute, the reset now works, so it is done here *together with* 0064 |
| Bootstrap records trusted tool directories in the config directory; hooks read them | Rejected: it adds a file every hook must read before it can trust anything, and a fault reading it needs its own fail-closed path. It would serve macOS and Homebrew, which are not tested platforms |
| Remove user-writable directories from the inherited PATH at runtime | Rejected: needs a `stat` per entry before PATH is safe, which itself runs from PATH. It also drops Homebrew, which is user-owned on Apple silicon, so it fails macOS anyway |
| Name every tool by absolute path (`/usr/bin/python3`) | Rejected: every call site in every hook changes, and a single missed one reopens the hole with no line a test can check |
| A test-only environment variable that overrides the reset | Rejected: any variable that changes the hook's PATH is one more way to plant a tool, and an agent session can reach some of the places that set environment |
| Leave stub cases calling the real hooks under a narrowed PATH | Rejected: the hook would find the real tool in `/usr/bin`, so a case meant to show a guard failing closed without `grep` would pass, or fail, for the wrong reason. Run unchanged against the new hooks, 21 cases went red; others stayed green without exercising the missing tool they name |

## Consequences

- A planted `python3`, `jq`, `git` or other tool earlier in PATH no longer reaches a hook.
- **CI no longer runs the hooks on the Python floor.** `ci.yml` puts setup-python 3.11 first on
  PATH, and the hooks now ignore it and use the runner's `/usr/bin/python3`, whose version and
  PyYAML the image decides. pr-body-check imports PyYAML. Restoring the floor for the hooks
  means changing `ci.yml` (SEC-7), for example pointing `/usr/bin/python3` at 3.11. That is a
  separate change for the operator to authorise, not part of this one. If the image's
  `/usr/bin/python3` lacks PyYAML, bootstrap now dies in CI and pr-body-check reports every body
  unchecked, so the gap is loud rather than silent.
- **Bootstrap checks two interpreters.** The `python3` first on PATH (bin/trimtab, the registry
  check) and the one in `/usr/bin:/bin` (the hooks) can now differ, and an install needs both at
  3.11 or newer with PyYAML. The die path for the second cannot be driven by a test without
  replacing `/usr/bin/python3`.
- **The base-expectations step in CI goes red on this PR.** It runs main's `tests/guard-tests.sh`
  against this PR's hooks. Main's stub cases narrow PATH from outside, which these hooks ignore.
  The workflow expects this when a base expectation legitimately changes: a human merges over it.
- **macOS loses tools outside `/usr/bin:/bin`.** There `/usr/bin` has no `jq`, and its `python3` is
  the Xcode shim (3.9, below the 3.11 floor). Homebrew's `/opt/homebrew/bin` is no longer seen.
  How each guard behaves there is untested. macOS was already an untested platform (README,
  Platforms).
- A tool installed only under `/usr/local/bin` is no longer found. Debian puts packaged tools in
  `/usr/bin`, and `gh` from its apt repository installs there too.
