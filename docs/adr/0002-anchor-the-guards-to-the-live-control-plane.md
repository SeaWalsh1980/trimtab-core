# 0002. Anchor the guards to the live control plane

## Status

Accepted.

## Context

`bootstrap.sh` installs Trimtab by linking directories and files into the
Claude Code config directory (`$CLAUDE_CONFIG_DIR`, by default `~/.claude`).
It links `hooks/`, `rules/`, `bin/`, `commands/`, `agents/`, `skills/`,
`output-styles/` and the instruction file `CLAUDE.md`, and it generates
`settings.json`. An edit to anything those links reach takes effect in the
next session. It needs no commit, no pull request and no CI, so it is a
quicker way to change what the guards permit than any reviewed route.

At first the guards did not protect the files that enforce them. Measured by
sending PreToolUse payloads to `guard-paths.sh`, an `Edit` was allowed against
a live hook, a live rule file, `~/.claude/settings.json`,
`settings.base.json` and `bootstrap.sh`, and so was a `sed -i` on a live hook.

The fix had to work around three facts:

- `guard-paths` checks an allow-list of safe names (`*.md`, `*.example`,
  `*.template`, `*.sample`) before its deny lists. Adding entries to the deny
  lists could therefore never protect a rule file or `machine.example.json`.
- Git worktrees live inside the checkout, under `.claude/worktrees/<name>/`,
  and pull requests are written there. A prefix on the whole checkout would
  block the route by which a guard gets fixed.
- `~/.claude` holds the agent's own working state as well as configuration:
  `projects/*/memory/`, `plans/`, `todos/`, `logs/`, `sessions/`. The agent
  writes these with ordinary tools.

The split into a base and an instance (ADR 0008) adds one more fact. The
mechanism links point into a pinned base snapshot, while `rules/` and the
instruction file point into the instance checkout. A protected set built from
the hooks link alone would leave the instance's doctrine and settings
unprotected.

## Decision

Protect the **live tree only**. The roots come from
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}`, never from the hook's own location. The
protected set is:

- **Under the config directory:** the prefixes `hooks/`, `rules/`, `bin/`,
  `commands/`, `agents/`, `skills/`, `output-styles/` and `plugins/`, named
  whether or not they exist; the exact files `settings.json`,
  `settings.local.json` and `CLAUDE.md`; and the directory itself, matched by
  equality only. Memory, plans and other session state stay writable.
- **Every link's realpath.** Each directory bootstrap links is resolved on
  its own, and so is the instruction-file link `~/.claude/CLAUDE.md`. Each
  resolved directory is a protected root. A link that exists but does not
  resolve blocks every call, because then the protected set cannot be
  worked out.
- **Each tree those links resolve into** adds the prefixes `hooks/`,
  `rules/`, `bin/`, `trimtab/` and `systemd/`, and these exact files:
  `CLAUDE.md`, `instance.json`, `settings.base.json`,
  `settings.instance.json`, `machine.json`, `machine.example.json`,
  `bootstrap.sh` and `guards/secrets.patterns`. Because these are exact
  files and not a prefix, `.claude/worktrees/` stays open.
- **The file `TRIMTAB_SECRET_PATTERNS` names**, both as written and as
  physically resolved. The value must be an absolute path, or every call
  blocks: guard-secrets opens it relative to the session's working
  directory, so a relative value has no fixed file to protect.
- **The snapshot store**, `${XDG_DATA_HOME:-~/.local/share}/trimtab/core`.
  It is always a protected prefix, whether or not it exists yet, both as
  written and physically, so the rollback snapshot is covered too.
- **The user systemd unit directory**, as a prefix, because a unit's
  `ExecStart=` runs outside every session guard.

The check runs as its own function, before the safe-name allow-list. Only
`Read` is exempt, so an empty, unknown or future tool name is checked by
default. In Bash, any reference to a protected path is blocked, including
read-only commands. Absolute paths are matched as substrings. Relative paths
are found by splitting the command on shell and code punctuation and
resolving each fragment.

Directory links are resolved with the `cd -P` and `pwd -P` builtins. A link
to a file cannot be resolved that way, so the instruction-file link is
followed with plain `readlink`, at most 40 hops. A missing `readlink`, a loop,
or a target that is not a file blocks the call (ADR 0001).

## Options considered

- **Protect every copy of the checkout, worktrees included.** Rejected.
  Pull requests are written in worktrees, so the guard would block its own
  repair. The only way past it would be the operator's override, and a hard
  control would become a habit of switching it off.
- **Derive the roots from the hook's `$BASH_SOURCE`.** Rejected. A hook read
  from a worktree would protect that worktree. What matters is what the
  running session loads, and the config directory decides that.
- **Add entries to the existing deny lists.** Rejected because it does not
  work: the allow-list runs first and lets `*.md` through.
- **In Bash, block only commands that look like they write.** Rejected.
  Deciding whether a command writes means inferring intent, and the list of
  ways to write is open-ended (`sed -i`, `tee`, `>`, `cp`, `patch`,
  `python3 -c`, `find -exec`). The set of protected paths is closed and can
  be listed, so the hard check goes on the path.
- **Block `Read` too.** Rejected. Maintainers have to read the guards, and
  Claude Code loads the rules into context anyway, so blocking reads would
  stop maintainers without stopping an attacker.
- **Protect the whole config directory as a prefix.** This shipped first and
  was rejected after it went live. Every `Write` to `projects/*/memory/`,
  `plans/`, `todos/` and `logs/` exited 2, which broke agent memory and plan
  mode.
- **Protect the whole directory, minus a list of state directories.**
  Rejected. An exclusion list fails in the dangerous direction. A new state
  directory is blocked until someone notices, and an exclusion that is too
  wide unprotects configuration. Listing the protected names fails the safe
  way: a gap is visible in review.
- **Protect nothing under the config directory, only the linked trees.**
  Rejected. `settings.json` and `plugins/` exist only there.
- **Protect only the instruction-file link, not its target.** Rejected. The
  target would stay writable whenever no other link resolves into its tree,
  which is exactly the layout after the base/instance split.
- **Resolve the file link in pure bash.** Rejected because it is not
  possible: no bash builtin reads a link's target.
- **An in-repository override for the CI job that runs the base branch's
  guard tests against a pull request's hooks** (a label, a marker file, a
  commit token). Rejected. An override in the repository can be reached by
  the agent the control constrains.

## Consequences

- Read-only shell commands against the live tree fail. `ls ~/.claude` and
  `cat` of a live hook both exit 2. The Read, Grep and Glob tools still work,
  and so does a worktree.
- Re-running `bootstrap.sh` is left to the operator, because it writes
  protected files.
- Matching is lexical. `..` in a target path is collapsed as text, so
  `<symlink>/../<protected name>` can miss. A path built from fragments
  inside a command also defeats the scan. Both are known gaps.
- Worktrees can be edited by design. They are only as safe as the
  pull-request gate around them. `git pull` and `bootstrap.sh` change
  protected files without any tool call, so the guard closes the local-edit
  shortcut, not the supply chain.
- The memory store is writable, so a compromised session can plant
  misleading memories. Accepted: memory is not executed, and protecting it
  would remove the feature.
- A configuration location that Claude Code adds later stays unprotected
  until someone names it.
- A host without `readlink` now blocks every call.
- Cost: on a sandbox laid out like a base/instance install, 200 `Read` calls
  per pass, the per-link resolution added about 7.8 ms at p50 and 10 ms at
  p95 (from about 11.0 to 18.8 ms). A first version that resolved each hop
  in a subshell cost more and was dropped.
