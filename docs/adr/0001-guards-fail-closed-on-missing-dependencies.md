# 0001. Guards fail closed on missing dependencies

## Status

Accepted.

## Context

Trimtab's guards are Claude Code PreToolUse hooks written in bash:
`guard-bash.sh`, `guard-paths.sh`, `guard-secrets.sh` and
`guard-security.sh`. Claude Code treats every hook exit code except 2 as
non-blocking. A guard that errors out, or that cannot run at all, therefore
looks exactly like a guard that allows everything. Each guard's header says it
fails closed: a payload it cannot understand must block.

At first only the JSON parser was checked. Each guard looked for `jq` or
`python3` and exited 2 when neither was present. The other external commands
were not checked, and each of them failed open. This was measured by running
the guards under `env -i PATH=<stub>`, where the stub `bin` held only the
commands named:

| Guard | PATH holds | Payload | Observed | Expected |
| --- | --- | --- | --- | --- |
| guard-secrets | bash, cat, head, tail, basename, sed, python3 | a PEM private-key header in the written content | `grep: command not found`, exit 0 | exit 2 |
| guard-secrets | bash, cat, grep, sed, python3 | the same | `head`/`tail: command not found`, exit 0 | exit 2 |
| guard-paths | bash, cat, grep, basename, head, tail, python3 | a read of `/x/.env` | `sed: command not found`, exit 0 | exit 2 |
| guard-bash | bash, cat, python3 | `terraform destroy` | exit 2 | exit 2 |

Each route had one cause:

- **`grep` in guard-secrets.** The content was filtered with
  `grep -v … || true`. The `|| true` swallowed the failure, so the filtered
  content came out empty and every later check found nothing. The result
  could not be told apart from clean content.
- **`head`/`tail` in guard-secrets, and `sed` in guard-paths.** The parser
  printed the path on one line and the content or command after it, and these
  commands split the lines apart. If one was missing, both fields came out
  empty, and an empty field is exactly what an allow looks like. `basename`
  was the same kind of fault, but it only voided the path rules.
- **Empty stdin.** This one needs no missing command. `jq` treats empty
  input as a successful parse of nothing, so an empty or truncated payload
  became an empty command, path and content, and was allowed. A missing
  `cat` ends up here too. Only the `python3` branch raised an error, and
  only by accident.

Each of these is a guard allowing the action it exists to refuse. That is the
most serious failure a guard can have, however unlikely the trigger. A stock
Linux machine always has `grep`, but that does not make a fail-open route
acceptable.

## Decision

Two rules apply to every guard.

**Remove a dependency wherever a bash expansion does the same work.** `head`,
`tail`, `basename` and `sed` were splitting lines and paths. `${v%%…}`,
`${v#…}` and `${v##*/}` do that without leaving the shell, and a dependency
that does not exist cannot go unchecked.

**Check what remains, and read its exit status honestly.** guard-secrets
checks for `grep` at startup, as the parser is checked, and exits 2 with a
named reason if it is missing. At every call site the guard also tells
grep's three answers apart: 0 is a match, 1 is no match, and 2 or more means
grep itself failed. A failure calls `fail_closed`. That function is separate
from `block`, because nothing was detected: the guard is refusing to answer,
not answering "no". Both blanket `|| true` uses are gone. Content reaches
`grep` through a here-string, not a pipe. Under `pipefail`, a pipe can
report the writer's SIGPIPE when `grep -q` exits early, so the here-string
makes sure the guard tests grep's own status.

Every guard blocks an empty payload before it parses anything, and its
message says why.

The same rule applies to everything added since. A signature pack that is
named but missing, an unreadable pattern file and a malformed pattern line
all block. guard-paths runs `readlink` to resolve the instruction-file link
(ADR 0002), and if `readlink` is missing, every call blocks.

## Options considered

- **Remove the avoidable dependencies, check the rest, and read grep's
  status (chosen).** It fixes every route measured. It shrinks the external
  surface instead of documenting it. The status check also catches a `grep`
  that runs and then fails (a bad regex, a permissions error, a broken
  locale), which a presence check cannot see.
- **Drop the `|| true` and change nothing else.** Rejected. The guards run
  `set -uo pipefail` without `-e`, so the failed assignment is simply
  ignored. The filtered content is still empty and the guard still allows.
  `head`, `tail` and `sed` are also left in place.
- **Check `head`, `tail`, `basename` and `sed` at startup instead of
  replacing them.** Rejected. It fixes the same cases but keeps four
  dependencies whose work bash already does. Each guard would also need its
  own list, kept in step with its code.
- **`set -e` in the guards.** Rejected. An unexpected non-zero status would
  become exit 1, which Claude Code treats as non-blocking. Any stray failure
  would then be a silent allow, which is worse than no change.
- **A launcher that checks the environment before running each guard.**
  Rejected. It adds a component to solve a problem that a `command -v`
  check already solves. If the launcher itself failed, the tool call would
  run unguarded.
- **Rely on review, with no test.** Rejected. A fix to a fail-open guard
  that no test covers can regress without anyone noticing.

## Consequences

- The guard test matrix has a section on unrunnable dependencies. For each
  case it builds a stub `bin` of symlinks holding only the dependencies under
  test, and runs the guard with that as its `PATH`. Twelve cases were added.
  Six of them (the three missing commands and the three empty payloads)
  failed before the change and pass after. Three cases check that the guards
  still behave normally with every dependency present, so the new cases
  cannot pass by blocking everything. CI runs the matrix twice, once with
  `jq` and once with `jq` removed so that the `python3` fallback is used.
- Accepted: a guard can now block for a reason that has nothing to do with
  what it guards. A broken `PATH` shows up as a refused tool call, and the
  `fail_closed` message names the cause. A guard that cannot scan must not
  report "clean".
- Not addressed: `cat` is still an unchecked dependency. It no longer opens
  a fail-open route, because an empty payload blocks, but the message then
  blames the payload, not the missing command.
- Not addressed: the bash version. One guard uses an expansion that needs
  bash 4. Stock macOS ships bash 3.2, where that line errors and the guard
  can fail open. Linux is the only tested platform, and the README says so.
