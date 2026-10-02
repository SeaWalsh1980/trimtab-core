# 0003. Install only from a verified checkout or snapshot

## Status

Accepted.

## Context

`bootstrap.sh` points the live tree at `$REPO`, which is wherever the script
happens to be:

```bash
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
```

Every install step uses it: the links under `~/.claude`, the instruction
file, the generated `settings.json` and the systemd units. A run from a git
worktree therefore installs that worktree as the live source of the guards.
Worktrees are disposable. Once one is deleted, `~/.claude/hooks` dangles.
Claude Code treats a hook that cannot run as non-blocking, so every guard
stops enforcing and nothing reports it.

This happened in practice. `bootstrap.sh --check` reported drift it could not
explain, and the live weekly-report units turned out to be linked into an
agent worktree whose branch had already merged. If `hooks/` had been linked
the same way, deleting merged worktrees would have switched the guards off.

A second defect made it worse. `~/.claude` respected `CLAUDE_CONFIG_DIR`, but
the timer step always wrote to `$HOME/.config/systemd/user`. A user timer
lives in the operator's own systemd session, which `CLAUDE_CONFIG_DIR`
cannot redirect. So a run that had declared itself a sandbox still rewired
the operator's systemd.

The split into a base and an instance (ADR 0008) adds a new requirement. The
installer lives in the public base, which is not on the machine until
something fetches it. The operator clones the instance, which pins the base
at a commit.

## Decision

**Refuse to install from a linked worktree.** The canonical checkout comes
from git itself (`git rev-parse --path-format=absolute --git-common-dir`). If
its parent is not `$REPO`, the run stops before writing anything. Both sides
are compared as physical paths. A first version compared a logical `$REPO`
with git's physical answer, and refused a correct clone that was reached
through a symlink. `--allow-worktree` is the operator's opt-in.

**A sandboxed run gets no timer.** If `CLAUDE_CONFIG_DIR` is set to anything
other than `$HOME/.claude`, the run is treated as a test or CI job and the
whole timer step is skipped. The unit directory is
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`.

**`--check` names where each link actually points.** A link into a deleted
worktree, a link into another checkout and a missing link each need a
different fix.

**Under the split, the instance's `bootstrap.sh` is a shim.** It holds no
install logic. It:

1. refuses to run from a worktree, as above;
2. reads and validates `instance.json`. The base pin must be a full 40-hex
   SHA, because a branch, tag or short SHA would float. The base repository
   must have the form `owner/name`, and the shim builds the clone URL from
   it itself, never taking a URL from the file;
3. makes sure the snapshot `${XDG_DATA_HOME:-~/.local/share}/trimtab/core/<sha>/`
   exists. If it is missing, the shim clones into a temporary directory
   beside the store, checks out the SHA, checks that HEAD equals the pin and
   the tree is clean, and renames it into place, which is atomic on one
   filesystem. If the snapshot exists but is dirty or at the wrong SHA, the
   shim stops. That is a sign of tampering, and nothing repairs it
   automatically;
4. runs `<snapshot>/bootstrap.sh --instance <instance root>`.

Before switching any link, the base installer checks the instance's doctrine
registry, and stops on failure, leaving the previous install live. It then
switches the links and runs every guard self-probe. Retention runs only
after the links, the probes and `--check` have all succeeded: the installer
keeps the live snapshot and the one before it, for rollback, and deletes
older ones. A failed install deletes nothing, and nothing else deletes
snapshots.

## Options considered

- **Refuse by default, with an opt-in (chosen).** The failure is silent and
  affects every guard, so a warning is not enough. Git already knows the
  answer, so the check costs one subprocess.
- **Warn and continue.** Rejected. The broken state lasted two days before
  anyone diagnosed it, and a warning printed among a run's success lines is
  effectively what the operator already had.
- **Protect the worktree path in `guard-paths`.** Rejected. The operator runs
  bootstrap in a terminal, not through a tool call, so the hook never sees
  it. Worktrees are also deliberately left editable (ADR 0002).
- **Install from `git rev-parse --show-toplevel`.** Rejected. It would
  silently install a different tree from the one the operator ran, and from
  a worktree it still gives the worktree.
- **Require an explicit `--repo <path>`.** Rejected. It burdens the common
  correct call to guard against a rare wrong one, and a mistyped path brings
  the original failure back.
- **Refuse when `$REPO` is not a git checkout.** Rejected. A tarball or
  copied tree is a legitimate install that git cannot vouch for.
- **Keep `$HOME/.config/systemd/user` and document it.** Rejected. It had
  already caught someone out.
- **Ship the base as a git submodule of the instance.** Rejected. Every
  instance worktree would need `submodule update`. It was not verified that
  a cloud routine's clone recurses into submodules. It would also nest the
  live base inside the instance tree, which makes the protected prefixes of
  ADR 0002 overlap.
- **Vendor a copy of the base into the instance.** Rejected. It duplicates
  code, makes every upgrade a large pull request, and puts code outside the
  base.
- **Install the base with pip into a virtualenv.** Rejected. It adds
  infrastructure, and Debian's externally-managed Python (PEP 668) gets in
  the way.

## Consequences

- An operator who means to install from a worktree, for example to test a
  bootstrap change, must pass `--allow-worktree`. A sandboxed run with a
  temporary `CLAUDE_CONFIG_DIR` is the usual way to try an unmerged change.
- `git` is a soft dependency of the worktree check. Where git is missing, or
  `$REPO` is not a checkout, the check cannot run and the install goes
  ahead. The check is aimed at the accidental worktree run, not at a
  determined operator.
- A sandboxed run never calls `systemctl` at all.
- `git pull` and direct edits still change the live tree without bootstrap
  seeing them (ADR 0002).
- Under the split, a base change reaches a machine only when an instance
  commit moves the pin. That is slower than a `git pull`, deliberately.
- Two snapshots stay on disk on each machine.
- Current state: the worktree refusal, the sandbox rule, `--check` reporting
  and the guard self-probes are in `bootstrap.sh` today. The shim, the
  snapshot install, `--instance` and retention arrive with the cutover to
  snapshot installs. Until then the base is published for reading, not for
  installing.
