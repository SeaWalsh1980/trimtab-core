# trimtab-core

The base of Trimtab: a harness for Claude Code and the loop that improves
it.

Trimtab does two jobs. It **guards** a Claude Code session: PreToolUse hooks
that block destructive commands, reads of credential files, secrets written
into files, and edits to the controls that review change (CI workflows,
CODEOWNERS, Claude Code settings and hooks). And it runs a **loop** over a
body of engineering rules: every rule is a *harness item* with a stable ID,
pull requests cite the items they applied and report friction against them,
and a retrospective tallies that evidence per ID and proposes changes to the
items that keep causing trouble.

## The base and an instance

Trimtab is split in two, and this repository is only one half.

- **The base** (this repository) holds the mechanism: the `trimtab` Python
  package and its CLI, the guards and hooks, the `/trimtab-*` commands, the
  `trimtab-*` agents, `bootstrap.sh`, the routine templates and the tests.
  It holds no rules and no deployment values: no repository names, no
  paths, no identities.
- **An instance** holds what is particular to one operator: the doctrine
  (`rules/`, from which `HARNESS.md` is generated), the list of consuming
  projects, settings choices and deployment values. An instance pins the
  base snapshot it runs.

The package never guesses which instance it serves. It takes the instance
root from `--instance`, then from `TRIMTAB_INSTANCE`, and otherwise stops
with an error naming both. It reads doctrine only from the instance and code
(agents, commands) only from its own checkout.

The decisions behind this layout, and behind the guards, are recorded in
[docs/adr/](docs/adr/).

## Status

Work in progress. This repository currently holds the extracted mechanism
and its tests. **The base installs an instance, never itself.** An instance
carries a copy of `shim/bootstrap.sh`, which fetches and verifies the base
snapshot the instance pins and runs its `bootstrap.sh`; that installer
requires `--instance <dir>`, and a run without it stops with a usage error.
A way to create a new instance is planned but not yet available. Until
then, the base is published for reading and review rather than for use.

## Platforms

Linux is the only tested platform. Known gaps, not yet handled:

- **macOS ships bash 3.2.** One guard uses a bash 4 expansion; on bash 3.2
  that line errors, and Claude Code treats any hook exit code other than 2
  as "allow". **On a stock Mac a guard can fail open, silently.** Use a
  bash 4 or later.
- macOS: `readlink -f` (bootstrap and two hooks) needs macOS 12.3 or later,
  and the weekly usage report's timer uses systemd, which macOS lacks.
- Native Windows: creating symlinks needs Developer Mode or administrator
  rights; Git for Windows defaults to `core.symlinks=false`, and Git Bash's
  `ln -s` copies by default, so the linked hooks directory would become a
  frozen copy; the hooks are bash; there is no systemd. WSL behaves as
  Linux and is expected to work, but is untested.

## Secret signature packs

`hooks/guard-secrets.sh` always applies its built-in signatures (PEM keys,
GCP service-account JSON, AWS, GitHub, Telegram, `sk-` keys, JWTs). Vendor
signatures ship as packs in `hooks/secrets.d/` (`<pack>.patterns`, one
`<ERE><TAB><label>` per line, with a split `<pack>.probe` sample that
bootstrap and the guard tests join and require to be blocked).

- With `TRIMTAB_SECRET_PACKS` unset or empty, **every shipped pack
  applies**. A machine that has not set the switch loses nothing.
- A comma-separated list (`TRIMTAB_SECRET_PACKS=zoho,google`) narrows it to
  those packs.
- `none` leaves only the built-ins.
- `TRIMTAB_SECRET_PATTERNS=<file>` adds private patterns in the same format.

A pack named but missing, an unreadable pattern file, or a malformed line
blocks every write until fixed: the guards fail closed.

## Running the tests

From the root of this checkout:

```bash
env -u TRIMTAB_INSTANCE python3 -m unittest discover -s tests/trimtab -q
```

Expected: `OK (skipped=4)`. The four skips are the *instance checks*: they
check an instance's own doctrine against its generated registry, and the
base has no `rules/`, so they skip here whether the variable is unset or
set. Any other skip, error or failure is a real problem. The variable is
unset so that the result does not depend on the shell or session it runs
in.

The guard matrix and the usage-report tests:

```bash
./tests/guard-tests.sh hooks
./tests/rule-usage-tests.sh
```

`guard-tests.sh` prints one `PASS` or `FAIL` line per case; none may fail.
One known gap: its `rules frontmatter` section checks an instance's
`rules/*.md`, and in the base, which has none, it errors and the script
reports one failure. Every guard case passes.
`tests/bootstrap-env-tests.sh` runs bootstrap into a temporary config
directory and checks the environment it writes.

The package needs Python 3 and PyYAML; the guards need bash 4 or later,
`grep`, and `jq` or `python3`.

## Contributing

There is no project instruction file in this repository yet; these notes
stand in for one.

- Open pull requests as drafts, and mark them ready when the branch is
  final.
- Nothing in this repository may name a deployment: no repository names
  other than this one, no home paths, no email addresses, no routine or
  environment IDs. Test fixtures use placeholders (`owner/repo`).
- Cite decisions by this repository's ADRs in `docs/adr/`, never by an
  instance's ADRs, plans or specs; where no ADR here records a decision,
  state the reason in place. Text read inside another repository
  (`agents/`, `commands/`, `templates/`, `routines/templates/`, CLI output)
  writes `trimtab-core ADR NNNN`, because that repository numbers its own
  ADRs; elsewhere, write `ADR NNNN` or the ADR's full file name.
  `tests/trimtab/test_doc_references.py` checks the numbers and paths.
- New code uses the Python standard library; PyYAML stays the only runtime
  dependency.
- Unit tests are hermetic and deterministic: fixture instances in a
  temporary directory, no network.
- Errors are typed. Guards fail closed; the two policy hooks
  (`doctrine-drift`, `pr-body-check`) fail open with a message.
- A change to a guard comes with cases in `tests/guard-tests.sh`, including
  the case that must still be blocked.

## Licence

MIT. See [LICENSE](LICENSE).
